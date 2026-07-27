"""Structured dictionary projections used by JSON-facing retrieval APIs."""
from __future__ import annotations

from ...common import *


class JsonContextRendererMixin:
    def _node_context_payload(self, node: MemoryNode, subgraph: MemorySubgraph) -> dict[str, Any]:
        ranked = next((item for item in subgraph.ranked_nodes if item.node.id == node.id), None)
        payload = {
            "id": node.id,
            "type": node.type,
            "label": node.label,
            "text": self._compact_text(node.text or ""),
            "canonical_key": node.canonical_key,
            "status": node.status,
            "salience": node.salience,
            "confidence": node.confidence,
            "properties": dict(node.properties),
        }
        if ranked is not None:
            payload["score"] = ranked.score
            payload["reasons"] = dict(ranked.reasons)
        return payload

    def _ranked_payload(self, item: RankedNode) -> dict[str, Any]:
        return {
            "id": item.node.id,
            "type": item.node.type,
            "label": item.node.label,
            "text": self._compact_text(item.node.text or item.node.label or item.node.canonical_key or ""),
            "score": item.score,
            "reasons": dict(item.reasons),
        }

    def _edge_context_payload(self, edge: MemoryEdge, nodes: OrderedDict[str, MemoryNode]) -> dict[str, Any]:
        from_label = self._node_label(nodes[edge.from_id]) if edge.from_id in nodes else edge.from_id
        to_label = self._node_label(nodes[edge.to_id]) if edge.to_id in nodes else edge.to_id
        return {
            "id": edge.id,
            "type": edge.type,
            "directed": True,
            "direction": "outgoing",
            "from_id": edge.from_id,
            "from_label": from_label,
            "to_id": edge.to_id,
            "to_label": to_label,
            "source_id": edge.from_id,
            "source_label": from_label,
            "target_id": edge.to_id,
            "target_label": to_label,
            "weight": edge.weight,
            "confidence": edge.confidence,
            "polarity": edge.polarity,
            "origin": edge.origin,
            "properties": dict(edge.properties),
        }

    def _edge_direction_index(
        self,
        nodes: OrderedDict[str, MemoryNode],
        edges: Any,
        *,
        per_node_limit: int = 12,
    ) -> dict[str, dict[str, Any]]:
        index: dict[str, dict[str, Any]] = {}
        for node_id, node in nodes.items():
            index[node_id] = {
                "node_id": node_id,
                "label": self._node_label(node),
                "incoming": [],
                "outgoing": [],
            }
        for edge in edges:
            if edge.from_id in index and edge.to_id in nodes and len(index[edge.from_id]["outgoing"]) < per_node_limit:
                index[edge.from_id]["outgoing"].append(self._direction_edge_ref(edge, nodes, direction="outgoing"))
            if edge.to_id in index and edge.from_id in nodes and len(index[edge.to_id]["incoming"]) < per_node_limit:
                index[edge.to_id]["incoming"].append(self._direction_edge_ref(edge, nodes, direction="incoming"))
        return {
            node_id: payload
            for node_id, payload in index.items()
            if payload["incoming"] or payload["outgoing"]
        }

    def _direction_edge_ref(
        self,
        edge: MemoryEdge,
        nodes: OrderedDict[str, MemoryNode] | dict[str, MemoryNode],
        *,
        direction: str,
    ) -> dict[str, Any]:
        other_id = edge.to_id if direction == "outgoing" else edge.from_id
        other = nodes.get(other_id)
        return {
            "edge_id": edge.id,
            "type": edge.type,
            "direction": direction,
            "from_id": edge.from_id,
            "to_id": edge.to_id,
            "other_id": other_id,
            "other_label": self._node_label(other) if other is not None else other_id,
            "weight": edge.weight,
            "confidence": edge.confidence,
        }

    def _direction_summary_lines(
        self,
        nodes: dict[str, MemoryNode],
        edges: list[MemoryEdge],
        *,
        max_items: int,
    ) -> list[str]:
        incoming: dict[str, list[MemoryEdge]] = {}
        outgoing: dict[str, list[MemoryEdge]] = {}
        for edge in edges:
            if edge.from_id in nodes and edge.to_id in nodes:
                outgoing.setdefault(edge.from_id, []).append(edge)
                incoming.setdefault(edge.to_id, []).append(edge)
        lines: list[str] = []
        for node_id, node in nodes.items():
            node_in = incoming.get(node_id, [])
            node_out = outgoing.get(node_id, [])
            if not node_in and not node_out:
                continue
            lines.append(f"- {self._node_label(node)}: {len(node_out)} outgoing, {len(node_in)} incoming")
            if len(lines) >= max_items:
                break
        return lines

    @staticmethod
    def _normalize_query_explore_views(views: Sequence[str] | None) -> tuple[str, ...]:
        if not views:
            return QUERY_EXPLORE_DEFAULT_VIEWS
        normalized: list[str] = []
        aliases = {
            "all": "__all__",
            "owner": "owners",
            "owners_only": "owners",
            "caller": "callers",
            "callers_only": "callers",
            "surface": "public_surface",
            "public": "public_surface",
            "public_only": "public_surface",
            "public_surface_only": "public_surface",
            "serialization": "serialization_paths",
            "serialization_only": "serialization_paths",
            "serialization_paths_only": "serialization_paths",
            "docs": "docs_mentions",
            "docs_only": "docs_mentions",
            "docs_mentions_only": "docs_mentions",
            "duplicates": "structural_duplicates",
            "structural": "structural_duplicates",
            "structural_duplicates_only": "structural_duplicates",
            "code_only": "code",
        }
        for view in views:
            key = str(view).strip().casefold().replace("-", "_")
            if not key:
                continue
            value = aliases.get(key, key)
            if value == "__all__":
                return QUERY_EXPLORE_ALL_VIEWS
            if value not in QUERY_EXPLORE_VIEWS:
                valid = ", ".join(sorted(QUERY_EXPLORE_VIEWS | {"all"}))
                raise ValueError(f"unknown query_explore view '{view}'. Choose from: {valid}")
            if value not in normalized:
                normalized.append(value)
        return tuple(normalized or QUERY_EXPLORE_DEFAULT_VIEWS)

    def _query_explore_seed_nodes(self, subgraph: MemorySubgraph, *, limit: int) -> OrderedDict[str, MemoryNode]:
        seeds: OrderedDict[str, MemoryNode] = OrderedDict()
        for node_id in subgraph.seed_node_ids:
            node = self.store.get_node(node_id)
            if node is not None and self._is_code_context_node(node):
                seeds[node.id] = node
            if len(seeds) >= limit:
                return seeds
        for item in subgraph.ranked_nodes:
            if self._is_code_context_node(item.node):
                seeds.setdefault(item.node.id, item.node)
            if len(seeds) >= limit:
                break
        return seeds

    def _query_explore_owners(
        self,
        seed_ids: set[str],
        nodes: OrderedDict[str, MemoryNode],
        edges: list[MemoryEdge],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen_nodes: set[str] = set()
        for node_id in seed_ids:
            node = nodes.get(node_id)
            if node is None or not self._is_owner_symbol_node(node):
                continue
            seen_nodes.add(node.id)
            rows.append(
                {
                    "role": "owner",
                    "owner": self._query_explore_node_payload(node),
                    "target": self._query_explore_node_payload(node),
                    "edge": None,
                    "reason": "seed owner symbol",
                }
            )
            if len(rows) >= limit:
                return rows
        seen: set[tuple[str, str]] = set()
        for edge in edges:
            if edge.type not in OWNER_EDGE_TYPES or edge.to_id not in seed_ids:
                continue
            owner = nodes.get(edge.from_id)
            target = nodes.get(edge.to_id)
            if owner is None or target is None:
                continue
            if owner.id in seen_nodes:
                continue
            if target.id in seen_nodes and owner.type in {"File", "SourceArtifact"}:
                continue
            key = (owner.id, edge.id)
            if key in seen:
                continue
            seen.add(key)
            seen_nodes.add(owner.id)
            rows.append(
                {
                    "role": "owner",
                    "owner": self._query_explore_node_payload(owner),
                    "target": self._query_explore_node_payload(target),
                    "edge": self._query_explore_edge_payload(edge, nodes),
                    "reason": f"{edge.type} incoming to seed",
                }
            )
            if len(rows) >= limit:
                break
        if rows:
            return rows
        for node_id in seed_ids:
            node = nodes.get(node_id)
            if node is None:
                continue
            rows.append(
                {
                    "role": "owner",
                    "owner": self._query_explore_node_payload(node),
                    "target": self._query_explore_node_payload(node),
                    "edge": None,
                    "reason": "seed owner symbol",
                }
            )
            if len(rows) >= limit:
                break
        return rows

    @staticmethod
    def _is_owner_symbol_node(node: MemoryNode) -> bool:
        return node.type in {
            "Module",
            "Function",
            "Class",
            "Interface",
            "Method",
            "Endpoint",
            "Schema",
            "Config",
            "StaticAnalysisFinding",
        }

    def _query_explore_callers(
        self,
        seed_ids: set[str],
        nodes: OrderedDict[str, MemoryNode],
        edges: list[MemoryEdge],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for edge in edges:
            if edge.type not in CALLER_EDGE_TYPES or edge.to_id not in seed_ids:
                continue
            caller = nodes.get(edge.from_id)
            target = nodes.get(edge.to_id)
            if caller is None or target is None or edge.id in seen:
                continue
            seen.add(edge.id)
            rows.append(
                {
                    "role": "caller",
                    "caller": self._query_explore_node_payload(caller),
                    "target": self._query_explore_node_payload(target),
                    "edge": self._query_explore_edge_payload(edge, nodes),
                    "reason": f"incoming {edge.type}",
                }
            )
            if len(rows) >= limit:
                break
        return rows

    def _query_explore_public_surface(
        self,
        seed_ids: set[str],
        nodes: OrderedDict[str, MemoryNode],
        edges: list[MemoryEdge],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[tuple[str, str | None]] = set()
        for edge in edges:
            if edge.type in PUBLIC_SURFACE_EDGE_TYPES and (edge.from_id in seed_ids or edge.to_id in seed_ids):
                node = nodes.get(edge.from_id if edge.from_id not in seed_ids else edge.to_id)
                if node is None:
                    continue
                key = (node.id, edge.id)
                if key in seen:
                    continue
                seen.add(key)
                rows.append(
                    {
                        "surface": self._query_explore_node_payload(node),
                        "edge": self._query_explore_edge_payload(edge, nodes),
                        "reason": f"{edge.type} near seed",
                    }
                )
            if len(rows) >= limit:
                return rows
        for node_id in seed_ids:
            node = nodes.get(node_id)
            if node is None or not self._is_public_surface_node(node):
                continue
            key = (node.id, None)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "surface": self._query_explore_node_payload(node),
                    "edge": None,
                    "reason": "seed is public API-shaped",
                }
            )
            if len(rows) >= limit:
                break
        return rows

    def _query_explore_serialization_paths(
        self,
        query: MemoryQuery,
        seed_ids: set[str],
        nodes: OrderedDict[str, MemoryNode],
        edges: list[MemoryEdge],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[tuple[str, str | None]] = set()
        for edge in edges:
            if edge.type not in SERIALIZATION_EDGE_TYPES:
                continue
            if edge.from_id not in seed_ids and edge.to_id not in seed_ids:
                continue
            other_id = edge.to_id if edge.from_id in seed_ids else edge.from_id
            node = nodes.get(other_id)
            if node is None:
                continue
            key = (node.id, edge.id)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "node": self._query_explore_node_payload(node),
                    "edge": self._query_explore_edge_payload(edge, nodes),
                    "reason": f"{edge.type} serialization-adjacent edge",
                }
            )
            if len(rows) >= limit:
                return rows
        return rows

    def _query_explore_docs_mentions(
        self,
        query: MemoryQuery,
        seed_ids: set[str],
        nodes: OrderedDict[str, MemoryNode],
        edges: list[MemoryEdge],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[tuple[str, str | None]] = set()
        for edge in edges:
            if edge.type not in {"REFERENCES", "EVIDENCED_BY", "DERIVED_FROM", "HAS_DOCSTRING", "HAS_COMMENT"}:
                continue
            if edge.from_id not in seed_ids and edge.to_id not in seed_ids:
                continue
            other_id = edge.to_id if edge.from_id in seed_ids else edge.from_id
            node = nodes.get(other_id)
            if node is None or not self._is_docs_mention_node(node):
                continue
            key = (node.id, edge.id)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "mention": self._query_explore_node_payload(node),
                    "edge": self._query_explore_edge_payload(edge, nodes),
                    "reason": f"{edge.type} linked source mention",
                }
            )
            if len(rows) >= limit:
                return rows
        for node, score in self.store.lexical_search(query.text, top_k=max(limit * 2, 20), node_types=set(SOURCE_NODE_TYPES), include_archived=query.include_archived):
            if not self._is_docs_mention_node(node):
                continue
            key = (node.id, None)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "mention": self._query_explore_node_payload(node),
                    "edge": None,
                    "score": score,
                    "reason": "document source lexical match",
                }
            )
            if len(rows) >= limit:
                break
        return rows

    def _query_explore_structural_duplicates(
        self,
        seed_nodes: Sequence[MemoryNode],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        seed_paths = {
            path
            for node in seed_nodes
            if (path := self._node_relative_path(node)) and self._is_template_path(path)
        }
        if not seed_paths:
            return []

        fragments: dict[str, list[MemoryNode]] = {}
        representatives: dict[str, MemoryNode] = {}
        for node in self.store.all_nodes():
            if node.status in INACTIVE_STATUSES:
                continue
            path = self._node_relative_path(node)
            if not path or not self._is_template_path(path):
                continue
            current = representatives.get(path)
            if current is None or self._template_representative_rank(node) > self._template_representative_rank(current):
                representatives[path] = node
            if node.type == "SourceFragment" and node.text:
                fragments.setdefault(path, []).append(node)

        signatures: dict[str, tuple[tuple[str, ...], set[str]]] = {}
        for path, items in fragments.items():
            ordered = sorted(items, key=lambda item: int(item.properties.get("line_start") or 0))
            signatures[path] = self._template_structure_signature("\n".join(item.text or "" for item in ordered))

        rows: list[dict[str, Any]] = []
        seen_pairs: set[tuple[str, str]] = set()
        for source_path in sorted(seed_paths):
            source_signature = signatures.get(source_path)
            source_node = representatives.get(source_path)
            if source_signature is None or source_node is None:
                continue
            source_sequence, source_features = source_signature
            if len(source_sequence) < 3 or not source_features:
                continue
            for duplicate_path, (duplicate_sequence, duplicate_features) in signatures.items():
                if duplicate_path == source_path or len(duplicate_sequence) < 3:
                    continue
                pair = tuple(sorted((source_path, duplicate_path)))
                if pair in seen_pairs:
                    continue
                shared = source_features & duplicate_features
                similarity = len(shared) / max(1, len(source_features | duplicate_features))
                if similarity < 0.50 or len(shared) < 2:
                    continue
                duplicate_node = representatives.get(duplicate_path)
                if duplicate_node is None:
                    continue
                seen_pairs.add(pair)
                shared_patterns = [
                    pattern
                    for prefix in ("sequence:", "edge:", "node:")
                    for pattern in sorted(item for item in shared if item.startswith(prefix))[:3]
                ]
                rows.append(
                    {
                        "source": self._query_explore_node_payload(source_node),
                        "duplicate": self._query_explore_node_payload(duplicate_node),
                        "similarity": round(similarity, 4),
                        "source_tag_count": len(source_sequence),
                        "duplicate_tag_count": len(duplicate_sequence),
                        "shared_patterns": shared_patterns[:8],
                        "reason": "similar markup hierarchy and tag sequence; lexical relevance is not used",
                    }
                )
        rows.sort(
            key=lambda row: (
                -float(row["similarity"]),
                str(row["source"].get("location") or ""),
                str(row["duplicate"].get("location") or ""),
            )
        )
        return rows[:limit]

    @staticmethod
    def _is_template_path(path: str) -> bool:
        value = path.replace("\\", "/").lstrip("/").casefold()
        suffix = Path(value).suffix
        if suffix in {".html", ".htm", ".twig", ".jinja", ".jinja2", ".hbs", ".mustache", ".vue"}:
            return True
        return suffix in {".php", ".phtml"} and any(part in f"/{value}" for part in ("/views/", "/templates/"))

    @staticmethod
    def _template_representative_rank(node: MemoryNode) -> int:
        return {"File": 3, "SourceArtifact": 2, "SourceFragment": 1}.get(node.type, 0)

    @staticmethod
    def _template_structure_signature(text: str) -> tuple[tuple[str, ...], set[str]]:
        tag_pattern = re.compile(r"<\s*(/?)\s*([A-Za-z][\w:-]*)\b([^>]*)>", re.IGNORECASE)
        attr_pattern = re.compile(r"\b([A-Za-z_:][\w:.-]*)\s*(?:=|\s|$)")
        void_tags = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
        stack: list[str] = []
        sequence: list[str] = []
        features: set[str] = set()
        for match in tag_pattern.finditer(text):
            closing, raw_tag, raw_attrs = match.groups()
            tag = raw_tag.casefold()
            if closing:
                if tag in stack:
                    while stack and stack[-1] != tag:
                        stack.pop()
                    if stack:
                        stack.pop()
                continue
            attrs = sorted(
                {
                    attr.casefold()
                    for attr in attr_pattern.findall(raw_attrs)
                    if attr.casefold() not in {"php", "echo"}
                }
            )
            parent = stack[-1] if stack else "root"
            descriptor = f"{len(stack)}:{tag}[{','.join(attrs)}]"
            sequence.append(descriptor)
            features.add(f"edge:{parent}>{tag}")
            features.add(f"node:{descriptor}")
            self_closing = raw_attrs.rstrip().endswith("/") or tag in void_tags
            if not self_closing:
                stack.append(tag)
        for index in range(max(0, len(sequence) - 2)):
            features.add("sequence:" + "/".join(sequence[index : index + 3]))
        return tuple(sequence), features

    def _query_explore_followups(self, query_text: str, views: Sequence[str], seed_nodes: OrderedDict[str, MemoryNode]) -> list[dict[str, str]]:
        query = self._reql_string(query_text)
        followups = [
                {
                    "label": "Full dependency slices",
                    "command": f"reql query_explore --query {query} --json",
                    "purpose": "all dependency-oriented views",
                },
                {
                    "label": "Structured graph",
                    "command": f"reql query_graph --query {query} --max-depth 3 --json",
                    "purpose": "expanded edge details",
                },
        ]
        if seed_nodes:
            first = next(iter(seed_nodes))
            followups.append(
                {
                    "label": "Inspect first seed",
                    "command": f"reql inspect --node-id {first} --json",
                    "purpose": "seed provenance and neighbors",
                }
            )
        for view in views:
            followups.append(
                {
                    "label": f"{view} only",
                    "command": f"reql query_explore --query {query} --view {view} --json",
                    "purpose": f"{view} view only",
                }
            )
            if len(followups) >= 6:
                break
        return followups

