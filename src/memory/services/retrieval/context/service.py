"""Orchestration service for structured and rendered retrieval context."""
from __future__ import annotations

from ..common import *


class ContextServiceMixin:
    def compose_context(
        self,
        subgraph: MemorySubgraph,
        *,
        max_items: int = 20,
        query_mode: str = "informative",
        query_scopes: Sequence[str] | None = None,
    ) -> str:
        """Render compact agent-ready context from a retrieved subgraph."""
        payload = self.query_context_payload(
            subgraph,
            max_items=max_items,
            query_mode=query_mode,
            query_scopes=query_scopes,
        )
        return self.render_context_payload(payload)

    def render_context_payload(self, payload: dict[str, Any]) -> str:
        """Render an already projected query-context payload."""
        return self._render_query_context_payload(payload)

    def query_context_payload(
        self,
        subgraph: MemorySubgraph,
        *,
        max_items: int = 20,
        query_mode: str = "informative",
        query_scopes: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Return compact structured agent context without duplicated rendered Markdown."""
        query_mode = self._normalize_query_context_mode(query_mode)
        scopes = self._normalize_query_context_scopes(query_scopes)
        subgraph = self._filter_query_context_subgraph(subgraph, scopes)
        explicit_code_scope = bool(scopes and scopes <= {"code", "test"})
        if explicit_code_scope or self._should_render_code_context(subgraph, max_items=max_items):
            payload = self._code_agent_context_payload(subgraph, query_mode=query_mode, max_items=max_items)
        else:
            payload = self._general_agent_context_payload(subgraph, query_mode=query_mode, max_items=max_items)
        visible_scores = (
            [float(item["score"]) for item in payload.get("results", []) if item.get("score") is not None]
            if scopes == {"docs"}
            else None
        )
        payload["confidence"] = self._query_context_confidence_payload(subgraph, visible_scores=visible_scores)
        payload["scopes"] = sorted(scopes)
        if query_mode == "informative" and not scopes:
            related_files = self._related_file_payload(subgraph, max_items=max_items)
            payload["related_files"] = related_files
            if related_files:
                payload["counts"]["related_files"] = len(related_files)
        return payload

    @staticmethod
    def _query_context_confidence_payload(
        subgraph: MemorySubgraph,
        *,
        visible_scores: Sequence[float] | None = None,
    ) -> dict[str, Any]:
        max_score = max(
            visible_scores if visible_scores is not None else (float(item.score) for item in subgraph.ranked_nodes),
            default=0.0,
        )
        sufficient = max_score >= QUERY_CONTEXT_MIN_CONFIDENCE_SCORE
        return {
            "status": "sufficient" if sufficient else "insufficient",
            "max_score": round(max_score, 4),
            "threshold": QUERY_CONTEXT_MIN_CONFIDENCE_SCORE,
            "targeted_rg_fallback_allowed": not sufficient,
            "reason": (
                "top ranked result meets the minimum confidence threshold"
                if sufficient
                else "top ranked result is below the minimum confidence threshold"
            ),
        }


    def query_graph(
        self,
        query: MemoryQuery,
        *,
        max_nodes: int = 80,
        max_edges: int = 160,
        max_sources: int = 20,
        max_items: int = 18,
        filter_generic: bool = True,
    ) -> dict[str, Any]:
        """Return a structured query-centered subgraph for agents and debugging."""
        max_nodes = max(1, max_nodes)
        max_edges = max(0, max_edges)
        max_sources = max(0, max_sources)
        retrieval = self.retrieve(query)

        seed_nodes: OrderedDict[str, MemoryNode] = OrderedDict()
        for node_id in retrieval.seed_node_ids:
            node = self.store.get_node(node_id)
            if node is None:
                continue
            if node.type not in GRAPH_SEED_NODE_TYPES or not self._is_graph_layer_node(node):
                continue
            if not query.include_archived and node.status in INACTIVE_STATUSES:
                continue
            seed_nodes[node.id] = node
            if len(seed_nodes) >= max(query.top_k, 1):
                break
        if not seed_nodes:
            for item in retrieval.ranked_nodes:
                if item.node.type not in GRAPH_SEED_NODE_TYPES or not self._is_graph_layer_node(item.node):
                    continue
                seed_nodes[item.node.id] = item.node
                if len(seed_nodes) >= max(query.top_k, 1):
                    break

        nodes: OrderedDict[str, MemoryNode] = OrderedDict()
        edges: OrderedDict[str, MemoryEdge] = OrderedDict()

        for node in retrieval.nodes:
            if not self._is_graph_layer_node(node):
                continue
            nodes.setdefault(node.id, node)
        for edge in retrieval.edges:
            if edge.type in TECHNICAL_EDGE_TYPES:
                continue
            edges.setdefault(edge.id, edge)

        traversal_edge_types = query.edge_types if query.edge_types is not None else DEFAULT_CONTEXT_EDGE_TYPES
        for seed_id in seed_nodes:
            expanded_nodes, expanded_edges = self.store.bounded_neighborhood(
                seed_id,

                max_depth=query.max_depth,
                edge_types=traversal_edge_types,
                limit=max_nodes,
            )
            for node in expanded_nodes:
                if not self._is_graph_layer_node(node):
                    continue
                if not query.include_archived and node.status in INACTIVE_STATUSES:
                    continue
                nodes.setdefault(node.id, node)
                if len(nodes) >= max_nodes:
                    break
            for edge in expanded_edges:
                if edge.type in TECHNICAL_EDGE_TYPES:
                    continue
                if query.edge_types and edge.type not in query.edge_types:
                    continue
                edges.setdefault(edge.id, edge)
                if len(edges) >= max_edges:
                    break
            if len(nodes) >= max_nodes and len(edges) >= max_edges:
                break

        for node in seed_nodes.values():
            nodes.setdefault(node.id, node)

        sources = self._collect_sources(nodes, edges, query=query, seed_nodes=seed_nodes, limit=max_sources)
        for source in sources.values():
            nodes.setdefault(source.id, source)

        filtered_node_ids: list[str] = []
        if filter_generic:
            nodes, edges, filtered_node_ids = self._filter_generic_context_nodes(
                nodes,
                edges,
                query_text=query.text,
                seed_nodes=set(seed_nodes),
                sources=set(sources),
            )

        if len(nodes) > max_nodes:
            nodes = OrderedDict(list(nodes.items())[:max_nodes])
        if len(edges) > max_edges:
            edges = OrderedDict(list(edges.items())[:max_edges])
        edges = OrderedDict((edge_id, edge) for edge_id, edge in edges.items() if edge.from_id in nodes and edge.to_id in nodes)

        node_payload = [self._node_context_payload(node, retrieval) for node in nodes.values()]
        edge_payload = [self._edge_context_payload(edge, nodes) for edge in edges.values() if edge.from_id in nodes and edge.to_id in nodes]
        edge_directions = self._edge_direction_index(nodes, edges.values())
        source_payload = [self._source_context_payload(node, nodes=nodes, edges=edges.values()) for node in sources.values()]
        ranked_nodes = [item for item in retrieval.ranked_nodes if self._is_graph_layer_node(item.node)]
        context = self.compose_query_graph(
            query.text,
            seed_nodes=list(seed_nodes.values()),
            ranked_nodes=ranked_nodes,
            nodes=list(nodes.values()),
            edges=list(edges.values()),
            sources=list(sources.values()),
            max_items=max_items,
        )

        return {
            "query": query.text,
            "parameters": {
                "top_k": query.top_k,
                "max_depth": query.max_depth,
                "max_nodes": max_nodes,
                "max_edges": max_edges,
                "max_sources": max_sources,
                "filter_generic": filter_generic,
            },
            "context": context,
            "seed_nodes": [self._node_context_payload(node, retrieval) for node in seed_nodes.values()],
            "ranked_nodes": [self._ranked_payload(item) for item in ranked_nodes],
            "nodes": node_payload,
            "edges": edge_payload,
            "edge_directions": edge_directions,
            "sources": source_payload,
            "filtered_node_ids": filtered_node_ids,
            "trace_id": retrieval.trace_id,
            "counts": {
                "seed_nodes": len(seed_nodes),
                "ranked_nodes": len(ranked_nodes),
                "nodes": len(node_payload),
                "edges": len(edge_payload),
                "nodes_with_directional_edges": len(edge_directions),
                "sources": len(source_payload),
                "filtered_nodes": len(filtered_node_ids),
            },
        }

    def query_explore(
        self,
        query: MemoryQuery,
        *,
        views: Sequence[str] | None = None,
        limit: int = 12,
        max_items: int = 18,
    ) -> dict[str, Any]:
        """Return dependency-oriented slices for coding agents."""
        requested_views = self._normalize_query_explore_views(views)
        limit = max(1, int(limit))
        max_items = max(1, int(max_items))
        code_query = MemoryQuery(
            text=query.text,
            top_k=query.top_k,
            max_depth=query.max_depth,
            min_activation=query.min_activation,
            include_archived=query.include_archived,
            node_types=set(CODE_CONTEXT_NODE_TYPES),
            edge_types=QUERY_EXPLORE_EDGE_TYPES,
            store_trace=query.store_trace,
        )
        subgraph = self.retrieve(code_query)
        seed_nodes = self._query_explore_seed_nodes(subgraph, limit=limit)
        seed_ids = set(seed_nodes)
        nodes: OrderedDict[str, MemoryNode] = OrderedDict(seed_nodes)
        for item in subgraph.ranked_nodes:
            if self._is_code_context_node(item.node):
                nodes.setdefault(item.node.id, item.node)
        for node in subgraph.nodes:
            if self._is_code_context_node(node) or node.type in SOURCE_NODE_TYPES:
                nodes.setdefault(node.id, node)

        incident_edges = self.store.incident_edges(
            list(seed_ids),
            edge_types=QUERY_EXPLORE_EDGE_TYPES,
            limit=max(1000, limit * 120),
        )
        for edge in incident_edges:
            for node_id in (edge.from_id, edge.to_id):
                if node_id not in nodes:
                    node = self.store.get_node(node_id)
                    if node is not None:
                        nodes[node.id] = node

        ranked_payloads = {
            item.node.id: self._agent_ranked_payload(item, max_text_chars=180)
            for item in subgraph.ranked_nodes
            if self._is_code_context_node(item.node)
        }
        code_payload = self._code_agent_context_payload(subgraph, query_mode="informative", max_items=max_items)
        sections: dict[str, Any] = {}
        if "owners" in requested_views:
            sections["owners"] = self._query_explore_owners(seed_ids, nodes, incident_edges, limit=limit)
        if "callers" in requested_views:
            sections["callers"] = self._query_explore_callers(seed_ids, nodes, incident_edges, limit=limit)
        if "public_surface" in requested_views:
            sections["public_surface"] = self._query_explore_public_surface(seed_ids, nodes, incident_edges, limit=limit)
        if "serialization_paths" in requested_views:
            sections["serialization_paths"] = self._query_explore_serialization_paths(query, seed_ids, nodes, incident_edges, limit=limit)
        if "docs_mentions" in requested_views:
            sections["docs_mentions"] = self._query_explore_docs_mentions(query, seed_ids, nodes, incident_edges, limit=limit)
        if "structural_duplicates" in requested_views:
            structural_seeds = [
                node
                for node_id in subgraph.seed_node_ids
                if (node := self.store.get_node(node_id)) is not None
            ]
            if not any(self._is_template_path(self._node_relative_path(node) or "") for node in structural_seeds):
                structural_seeds = list(seed_nodes.values())
            sections["structural_duplicates"] = self._query_explore_structural_duplicates(structural_seeds, limit=limit)
        if "code" in requested_views:
            sections["code"] = {
                "working_set": code_payload.get("working_set", [])[: min(max_items, limit)],
                "read_plan": code_payload.get("read_plan", [])[: min(max_items, limit)],
                "change_chain": code_payload.get("change_chain", [])[: min(max_items, limit)],
                "targeted_reads": code_payload.get("targeted_reads", [])[: min(max_items, limit)],
                "snippets": code_payload.get("snippets", [])[: min(max_items, limit)],
                "symbols": code_payload.get("symbols", [])[: min(max_items, limit)],
                "code_links": code_payload.get("code_links", [])[: min(max_items, limit)],
            }

        payload = {
            "kind": "query_explore",
            "query": query.text,
            "views": list(requested_views),
            "seed_nodes": [self._query_explore_node_payload(node, ranked_payloads.get(node.id)) for node in seed_nodes.values()],
            "sections": sections,
            "followups": self._query_explore_followups(query.text, requested_views, seed_nodes),
            "counts": {
                "seed_nodes": len(seed_nodes),
                "incident_edges": len(incident_edges),
                **{view: len(value) if isinstance(value, list) else sum(len(item) for item in value.values() if isinstance(item, list)) for view, value in sections.items()},
            },
            "trace_id": subgraph.trace_id,
        }
        payload["context"] = self._render_query_explore_payload(payload)
        return payload

    def query_context(
        self,
        query: MemoryQuery,
        *,
        max_items: int = 20,
        query_mode: str = "informative",
        query_scopes: Sequence[str] | None = None,
    ) -> str:
        """Return the compact deterministic context block for a query."""
        scoped_query = replace(query, context_scopes=set(query_scopes) if query_scopes else query.context_scopes)
        return self.compose_context(
            self.retrieve(scoped_query),
            max_items=max_items,
            query_mode=query_mode,
            query_scopes=query_scopes,
        )

    def query_memories(
        self,
        query: MemoryQuery,
        *,
        limit: int = 12,
        include_sources: bool = True,
        filter_generic: bool = True,
        max_text_chars: int = 600,
    ) -> list[dict[str, Any]]:
        """Return a compact list of relevant memory texts for a query."""
        return self.query_memories_payload(
            query,
            limit=limit,
            include_sources=include_sources,
            filter_generic=filter_generic,
            max_text_chars=max_text_chars,
        )["memories"]

    def query_memories_payload(
        self,
        query: MemoryQuery,
        *,
        limit: int = 12,
        include_sources: bool = True,
        filter_generic: bool = True,
        max_text_chars: int = 600,
    ) -> dict[str, Any]:
        """Return compact memory rows plus useful retrieval metadata."""
        limit = max(1, limit)
        max_text_chars = max(80, max_text_chars)
        retrieval = self.retrieve(query)
        memories = self._query_memory_rows(
            retrieval,
            limit=limit,
            include_sources=include_sources,
            filter_generic=filter_generic,
            max_text_chars=max_text_chars,
        )
        nodes = OrderedDict((node.id, node) for node in retrieval.nodes if self._is_graph_layer_node(node))
        edges = OrderedDict(
            (edge.id, edge)
            for edge in retrieval.edges
            if edge.type not in TECHNICAL_EDGE_TYPES and edge.from_id in nodes and edge.to_id in nodes
        )
        sources = OrderedDict(
            (node.id, node)
            for node in nodes.values()
            if node.type in SOURCE_NODE_TYPES and any(item["id"] == node.id for item in memories)
        )
        return {
            "query": query.text,
            "parameters": {
                "top_k": query.top_k,
                "max_depth": query.max_depth,
                "limit": limit,
                "include_sources": include_sources,
                "filter_generic": filter_generic,
                "max_text_chars": max_text_chars,
                "include_archived": query.include_archived,
            },
            "count": len(memories),
            "memories": memories,
            "ranked_nodes": [self._ranked_payload(item) for item in retrieval.ranked_nodes if self._is_graph_layer_node(item.node)],
            "nodes": [self._query_memory_node_payload(node, retrieval) for node in nodes.values()],
            "edges": [self._edge_context_payload(edge, nodes) for edge in edges.values()],
            "edge_directions": self._edge_direction_index(nodes, edges.values()),
            "sources": [self._query_memory_source_payload(node, nodes=nodes, edges=edges.values()) for node in sources.values()],
            "seed_node_ids": list(retrieval.seed_node_ids),
            "trace_id": retrieval.trace_id,
            "counts": {
                "memories": len(memories),
                "ranked_nodes": len(retrieval.ranked_nodes),
                "context_nodes": len(nodes),
                "edges": len(edges),
                "sources": len(sources),
                "seed_nodes": len(retrieval.seed_node_ids),
            },
        }

    def _query_memory_node_payload(self, node: MemoryNode, subgraph: MemorySubgraph) -> dict[str, Any]:
        ranked = next((item for item in subgraph.ranked_nodes if item.node.id == node.id), None)
        payload: dict[str, Any] = {
            "id": node.id,
            "type": node.type,
            "label": node.label,
            "text": self._compact_text(node.text or node.label or node.canonical_key or "", max_chars=260),
            "canonical_key": node.canonical_key,
            "status": node.status,
            "location": self._location_summary(node),
        }
        if ranked is not None:
            payload["score"] = ranked.score
            payload["reasons"] = dict(ranked.reasons)
        return payload

    def _query_memory_source_payload(
        self,
        node: MemoryNode,
        *,
        nodes: OrderedDict[str, MemoryNode] | dict[str, MemoryNode],
        edges: Any,
    ) -> dict[str, Any]:
        return {
            "id": node.id,
            "type": node.type,
            "label": node.label,
            "text": self._compact_text(node.text or node.label or node.canonical_key or "", max_chars=600),
            "location": self._location_summary(node),
            "source_for": self._source_relation_payload(node, nodes, edges),
        }

    def _query_memory_rows(
        self,
        retrieval: MemorySubgraph,
        *,
        limit: int,
        include_sources: bool,
        filter_generic: bool,
        max_text_chars: int,
    ) -> list[dict[str, Any]]:
        query = retrieval.query
        query_tokens = set(_expanded_tokens(query.text))
        degree: dict[str, int] = {}
        for edge in retrieval.edges:
            degree[edge.from_id] = degree.get(edge.from_id, 0) + 1
            degree[edge.to_id] = degree.get(edge.to_id, 0) + 1

        memories: list[dict[str, Any]] = []
        seen_texts: set[str] = set()
        rank_by_node_id = {item.node.id: index + 1 for index, item in enumerate(retrieval.ranked_nodes)}
        nodes_by_id = {node.id: node for node in retrieval.nodes}

        def add_memory(
            node: MemoryNode,
            *,
            score: float,
            reasons: dict[str, float] | None = None,
            source_for: str | None = None,
            relation: str | None = None,
            direction: str | None = None,
            edge_id: str | None = None,
        ) -> None:
            if len(memories) >= limit:
                return
            if not query.include_archived and node.status in INACTIVE_STATUSES:
                return
            if node.type in TECHNICAL_NODE_TYPES:
                return
            if source_for is None and node.type in SOURCE_NODE_TYPES and self._has_source_relation(node.id):
                return
            if source_for is None and not self._is_relevant_memory_node(node, query.text, query_tokens=query_tokens):
                return
            if filter_generic and source_for is None and self._is_generic_context_node(node, degree.get(node.id, 0)):
                return
            text = self._compact_text(node.text or node.label or node.canonical_key or "", max_chars=max_text_chars)
            if not text:
                return
            dedupe_key = " ".join(text.casefold().split())
            if node.type in SOURCE_NODE_TYPES:
                dedupe_key = f"source-node:{node.id}:{dedupe_key}"
            elif source_for is not None:
                dedupe_key = f"source:{source_for}:{relation or ''}:{direction or ''}:{dedupe_key}"
            if dedupe_key in seen_texts:
                return
            seen_texts.add(dedupe_key)
            source_node = nodes_by_id.get(source_for or "") if source_for else None
            memories.append(
                {
                    "id": node.id,
                    "type": node.type,
                    "label": node.label,
                    "text": text,
                    "canonical_key": node.canonical_key,
                    "status": node.status,
                    "location": self._location_summary(node),
                    "score": score,
                    "rank": rank_by_node_id.get(node.id),
                    "seed": node.id in retrieval.seed_node_ids,
                    "source_for": source_for,
                    "source_for_type": source_node.type if source_node else None,
                    "source_for_label": self._node_label(source_node) if source_node else None,
                    "relation": relation,
                    "direction": direction,
                    "edge_id": edge_id,
                    "reasons": dict(reasons or {}),
                    "properties": dict(node.properties),
                }
            )

        for item in retrieval.ranked_nodes:
            add_memory(item.node, score=item.score, reasons=item.reasons)
            if len(memories) >= limit:
                break
            if not include_sources:
                continue
            for edge, neighbor in self.store.neighbors(item.node.id, direction="both", edge_types=SOURCE_EDGE_TYPES, limit=20):
                if edge.type in TECHNICAL_EDGE_TYPES or neighbor.type in TECHNICAL_NODE_TYPES:
                    continue
                if neighbor.type not in SOURCE_NODE_TYPES:
                    continue
                if not self._source_is_query_relevant(neighbor, query=query, edges=OrderedDict([(edge.id, edge)]), seed_nodes=OrderedDict([(item.node.id, item.node)]), query_tokens=query_tokens):
                    continue
                direction = "outgoing" if edge.from_id == item.node.id else "incoming"
                add_memory(neighbor, score=item.score * 0.95, reasons=item.reasons, source_for=item.node.id, relation=edge.type, direction=direction, edge_id=edge.id)
                if len(memories) >= limit:
                    break

        if include_sources and len(memories) < limit:
            node_ids = [item.node.id for item in retrieval.ranked_nodes]
            for edge in retrieval.edges:
                if edge.type not in SOURCE_EDGE_TYPES:
                    continue
                if edge.type in TECHNICAL_EDGE_TYPES:
                    continue
                source_id = edge.from_id if edge.from_id not in node_ids else edge.to_id
                source = self.store.get_node(source_id)
                if source is None or source.type not in SOURCE_NODE_TYPES:
                    continue
                ranked_seed_nodes = OrderedDict((item.node.id, item.node) for item in retrieval.ranked_nodes)
                if not self._source_is_query_relevant(source, query=query, edges=OrderedDict([(edge.id, edge)]), seed_nodes=ranked_seed_nodes, query_tokens=query_tokens):
                    continue
                context_node_id = edge.to_id if source_id == edge.from_id else edge.from_id
                direction = "outgoing" if edge.from_id == context_node_id else "incoming"
                add_memory(source, score=0.5 * edge.weight, source_for=context_node_id, relation=edge.type, direction=direction, edge_id=edge.id)
                if len(memories) >= limit:
                    break

        return memories


    def compose_query_graph(
        self,
        query_text: str,
        *,
        seed_nodes: list[MemoryNode],
        ranked_nodes: list[RankedNode],
        nodes: list[MemoryNode],
        edges: list[MemoryEdge],
        sources: list[MemoryNode],
        max_items: int = 18,
    ) -> str:
        lines = [f"# REQL Query Graph", "", f"Query: {query_text}", ""]
        if seed_nodes:
            lines.append("## Seed nodes")
            for node in seed_nodes[:max_items]:
                lines.append(f"- {node.id} [{node.type}] {self._node_label(node)}")
            lines.append("")
        if ranked_nodes:
            lines.append("## Ranked relevance")
            for item in ranked_nodes[:max_items]:
                lines.append(f"- {item.score:.2f} {item.node.id} [{item.node.type}] {self._node_label(item.node)}")
            lines.append("")
        if edges:
            node_by_id = {node.id: node for node in nodes}
            lines.append("## Directed graph edges")
            emitted = 0
            for edge in edges:
                if edge.from_id not in node_by_id or edge.to_id not in node_by_id:
                    continue
                source = self._node_label(node_by_id[edge.from_id])
                target = self._node_label(node_by_id[edge.to_id])
                lines.append(f"- {source} --{edge.type}--> {target} (outgoing from source, incoming to target)")
                emitted += 1
                if emitted >= max_items:
                    break
            lines.append("")
            direction_lines = self._direction_summary_lines(node_by_id, edges, max_items=max_items)
            if direction_lines:
                lines.append("## Node edge direction")
                lines.extend(direction_lines)
                lines.append("")
        if sources:
            lines.append("## Textual sources")
            node_by_id = {node.id: node for node in nodes}
            for source in sources[:max_items]:
                text = self._compact_text(source.text or source.label or source.canonical_key or source.id, max_chars=240)
                refs = self._source_relation_refs(source, node_by_id, edges, limit=2)
                suffix = f" ({'; '.join(refs)})" if refs else ""
                lines.append(f"- {source.id} [{source.type}]{suffix} {text}")
            lines.append("")
        lines.append("## Counts")
        lines.append(f"- nodes: {len(nodes)}")
        lines.append(f"- edges: {len(edges)}")
        lines.append(f"- sources: {len(sources)}")
        return "\n".join(lines).strip()
