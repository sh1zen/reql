"""General and document context projections."""
from __future__ import annotations

from ...common import *


class GeneralContextProjectionMixin:
    def _related_file_payload(self, subgraph: MemorySubgraph, *, max_items: int) -> list[dict[str, Any]]:
        """Correlate documentation, implementation, and translation files deterministically."""
        profile = self._query_profile(subgraph.query.text)
        if not profile.informative_tokens:
            return []

        candidates: dict[str, MemoryNode] = {node.id: node for node in subgraph.nodes}
        for node, _score in self.store.lexical_search(
            subgraph.query.text,
            top_k=max(max_items * 8, 40),
            include_archived=subgraph.query.include_archived,
        ):
            candidates[node.id] = node

        project_ids = {
            str(node.properties.get("project_id"))
            for node in candidates.values()
            if node.properties.get("project_id") and self._node_query_token_overlap_tokens(node, profile.informative_tokens)
        }
        grouped: dict[tuple[str | None, str], list[tuple[MemoryNode, set[str]]]] = {}
        for node in candidates.values():
            if node.type in TECHNICAL_NODE_TYPES or node.status in INACTIVE_STATUSES:
                continue
            node_project_id = node.properties.get("project_id")
            if project_ids and node_project_id not in project_ids:
                continue
            path = self._node_relative_path(node)
            if not path or self._related_file_role(node, path) not in {"documentation", "implementation"}:
                continue
            overlap = self._node_query_token_overlap_tokens(node, profile.informative_tokens)
            if not overlap:
                continue
            project_id = str(node_project_id) if node_project_id is not None else None
            grouped.setdefault((project_id, path), []).append((node, overlap))

        rows: list[dict[str, Any]] = []
        for (project_id, path), matches in grouped.items():
            role = self._related_file_role(matches[0][0], path)
            best_node, best_overlap = max(
                matches,
                key=lambda item: (
                    len(item[1]),
                    self._line_span(item[0])[0] is not None,
                    item[0].type in SOURCE_NODE_TYPES,
                    item[0].salience,
                ),
            )
            line_start, line_end = self._line_span(best_node)
            rows.append(
                {
                    "action": "open" if role == "documentation" else "review",
                    "role": role,
                    "project_id": project_id,
                    "path": path,
                    "line_start": line_start,
                    "line_end": line_end,
                    "matched_terms": sorted(best_overlap),
                    "node_ids": sorted({node.id for node, _overlap in matches})[:4],
                    "reason": "FAQ documentation match" if role == "documentation" else "the same query terms appear in application code",
                }
            )

        correlated_terms: dict[str | None, set[str]] = {}
        for project_id in {row["project_id"] for row in rows}:
            docs_terms = set().union(
                *(set(row["matched_terms"]) for row in rows if row["project_id"] == project_id and row["role"] == "documentation")
            )
            code_terms = set().union(
                *(set(row["matched_terms"]) for row in rows if row["project_id"] == project_id and row["role"] == "implementation")
            )
            shared = docs_terms & code_terms
            if shared:
                correlated_terms[project_id] = shared
        if not correlated_terms:
            return []
        rows = [
            row
            for row in rows
            if row["project_id"] in correlated_terms and set(row["matched_terms"]) & correlated_terms[row["project_id"]]
        ]
        for row in rows:
            if "faq" in correlated_terms[row["project_id"]]:
                row["reason"] = "FAQ documentation match" if row["role"] == "documentation" else "the same FAQ terms appear in application code"

        translation_rows: list[dict[str, Any]] = []
        for project_id in sorted(value for value in correlated_terms if value is not None):
            artifacts = self.store.find_nodes_by_property(
                "project_id",
                project_id,
                type_="SourceArtifact",
                limit=10000,
                clone=False,
            )
            for artifact in artifacts:
                if artifact.status in INACTIVE_STATUSES:
                    continue
                path = self._node_relative_path(artifact)
                if not path or self._related_file_role(artifact, path) != "translation_catalog":
                    continue
                translation_rows.append(
                    {
                        "action": "update",
                        "role": "translation_catalog",
                        "project_id": project_id,
                        "path": path,
                        "line_start": None,
                        "line_end": None,
                        "matched_terms": sorted(correlated_terms[project_id]),
                        "node_ids": [artifact.id],
                        "reason": "translation catalog for the same project; synchronize translatable strings after code changes",
                    }
                )

        role_order = {"documentation": 0, "implementation": 1, "translation_catalog": 2}
        rows.extend(translation_rows)
        rows.sort(key=lambda row: (role_order.get(str(row["role"]), 9), str(row["path"]).casefold()))
        selected = rows[: min(max_items, 8)]
        for row in selected:
            row.pop("project_id", None)
        return selected

    @staticmethod
    def _related_file_role(node: MemoryNode, path: str) -> str | None:
        normalized = path.replace("\\", "/").casefold()
        suffix = Path(normalized).suffix
        if suffix in {".pot", ".po"}:
            return "translation_catalog"
        scope = str(node.properties.get("context_scope") or "").casefold()
        if scope in {"code", "test"} or suffix in {".php", ".py", ".js", ".ts", ".java", ".rb", ".go", ".rs", ".cs"}:
            return "implementation"
        name = normalized.rsplit("/", 1)[-1]
        if scope == "docs" or name.startswith(("readme", "changelog")) or suffix in {".md", ".rst", ".txt"}:
            return "documentation"
        return None

    def _general_agent_context_payload(
        self,
        subgraph: MemorySubgraph,
        *,
        query_mode: str,
        max_items: int,
    ) -> dict[str, Any]:
        buckets: dict[str, list[RankedNode]] = {
            "StaticAnalysisFinding": [],
        }
        for item in subgraph.ranked_nodes:
            if item.node.type in buckets:
                buckets[item.node.type].append(item)
        has_direct_general_evidence = self._has_direct_general_evidence(subgraph)
        document_scope = self._normalize_query_context_scopes(subgraph.query.context_scopes) == {"docs"}
        best_items = (
            self._document_best_match_items(subgraph, max_items=max_items)
            if document_scope
            else self._general_best_match_items(subgraph, max_items=max_items, prefer_general=has_direct_general_evidence)
        )
        source_items = self._agent_source_payloads(subgraph, max_items=max_items, query_text=subgraph.query.text)
        best_payloads = [self._agent_ranked_payload(item, max_text_chars=220) for item in best_items]
        cleanup_payloads = [self._agent_ranked_payload(item, max_text_chars=260) for item in buckets["StaticAnalysisFinding"]]
        cleanup_candidates = self._filter_cleanup_candidate_payloads(cleanup_payloads)[: min(max_items, 5)]
        return {
            "kind": "general",
            "query": subgraph.query.text,
            "query_mode": query_mode,
            "results": self._general_result_payloads(best_payloads, source_items, max_items=max_items),
            "cleanup_candidates": cleanup_candidates,
            "cleanup_filter": self._cleanup_filter_payload(
                total_candidates=len(cleanup_payloads),
                shown_candidates=len(cleanup_candidates),
            ) if query_mode == "cleanup" else {},
            "graph_links": self._agent_edge_lines(subgraph, max_items=max_items, hide_raw_event_links=True, hide_test_code_links=has_direct_general_evidence),
            "followups": self._agent_follow_up_payload(subgraph, max_items=max_items, ranked_items=best_items),
            "counts": {
                "ranked_nodes": len(subgraph.ranked_nodes),
                "context_nodes": len(subgraph.nodes),
                "edges": len(subgraph.edges),
            },
            "trace_id": subgraph.trace_id,
        }

    def _general_best_match_items(self, subgraph: MemorySubgraph, *, max_items: int, prefer_general: bool = False) -> list[RankedNode]:
        limit = min(max_items, 20)
        selected: list[RankedNode] = []
        seen: set[str] = set()
        query_tokens = set(_expanded_tokens(subgraph.query.text))
        deferred_sources: list[RankedNode] = []
        deferred_indirect: list[RankedNode] = []
        for item in subgraph.ranked_nodes:
            node = item.node
            if node.type == "RawEvent":
                continue
            path = self._node_relative_path(node) or ""
            if prefer_general and self._is_test_context_path(path) and self._is_code_context_node(node):
                deferred_indirect.append(item)
                continue
            searchable = " ".join(part for part in (node.label, node.text, node.canonical_key) if part)
            has_query_token = bool(query_tokens & set(_expanded_tokens(searchable)))
            if query_tokens and not has_query_token and float(item.reasons.get("match_score", 0.0) or 0.0) <= 0.0:
                deferred_indirect.append(item)
                continue
            if node.type in SOURCE_NODE_TYPES:
                deferred_sources.append(item)
                continue
            text = self._compact_text(node.text or node.label or node.canonical_key or "", max_chars=220)
            location = self._location_summary(node) or ""
            dedupe_key = f"{node.type}|{location}|{' '.join(text.casefold().split())}"
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            selected.append(item)
            if len(selected) >= limit:
                return selected
        for item in deferred_sources:
            text = self._compact_text(item.node.text or item.node.label or item.node.canonical_key or "", max_chars=220)
            location = self._location_summary(item.node) or ""
            dedupe_key = f"{item.node.type}|{location}|{' '.join(text.casefold().split())}"
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            selected.append(item)
            if len(selected) >= limit:
                return selected
        if prefer_general and selected:
            return selected
        for item in deferred_indirect:
            text = self._compact_text(item.node.text or item.node.label or item.node.canonical_key or "", max_chars=220)
            location = self._location_summary(item.node) or ""
            dedupe_key = f"{item.node.type}|{location}|{' '.join(text.casefold().split())}"
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            selected.append(item)
            if len(selected) >= limit:
                return selected
        return selected

    def _document_best_match_items(self, subgraph: MemorySubgraph, *, max_items: int) -> list[RankedNode]:
        """Rank visible document concepts by their strongest bounded evidence."""
        limit = min(max_items, 20)
        query_profile = self._query_profile(subgraph.query.text)
        nodes = {node.id: node for node in subgraph.nodes}
        nodes.update({item.node.id: item.node for item in subgraph.ranked_nodes})
        evidence_by_concept: dict[str, list[MemoryNode]] = {}
        for edge in subgraph.edges:
            if edge.type != "EVIDENCED_BY":
                continue
            concept = nodes.get(edge.from_id)
            evidence = nodes.get(edge.to_id)
            if concept is None or evidence is None or evidence.type != "RawEvent":
                continue
            if concept.type == "Concept" and evidence.status not in INACTIVE_STATUSES:
                evidence_by_concept.setdefault(concept.id, []).append(evidence)

        candidates: list[RankedNode] = []
        for item in subgraph.ranked_nodes:
            node = item.node
            if node.type == "RawEvent" or node.type in {"SourceArtifact", "File"}:
                continue
            metrics = self._node_match_metrics(node, query_profile)
            display_node = node
            evidence_score = 0.0
            label_score = 0.0
            if node.type == "Concept" and node.properties.get("extractor") == "document_processor":
                label_node = replace(node, text="", canonical_key=node.label or node.canonical_key)
                label_metrics = self._node_match_metrics(label_node, query_profile)
                label_score = float(label_metrics["match_score"])
                if (
                    float(label_metrics["match_score"]),
                    float(label_metrics["coverage"]),
                ) > (
                    float(metrics["match_score"]),
                    float(metrics["coverage"]),
                ):
                    metrics = label_metrics
                best_evidence: tuple[float, float, MemoryNode, dict[str, float]] | None = None
                for evidence in evidence_by_concept.get(node.id, []):
                    evidence_metrics = self._node_match_metrics(evidence, query_profile)
                    evidence_rank = (
                        float(evidence_metrics["match_score"]),
                        float(evidence_metrics["coverage"]),
                    )
                    if best_evidence is None or evidence_rank > best_evidence[:2]:
                        best_evidence = (*evidence_rank, evidence, evidence_metrics)
                if best_evidence is not None and best_evidence[0] > float(metrics["match_score"]):
                    evidence_score, _, evidence, metrics = best_evidence
                    display_node = replace(
                        node,
                        text=evidence.text,
                        properties={
                            **node.properties,
                            "line_start": evidence.properties.get("line_start"),
                            "line_end": evidence.properties.get("line_end"),
                            "evidence_node_id": evidence.id,
                        },
                    )
            match_score = float(metrics["match_score"])
            coverage = float(metrics["coverage"])
            if match_score <= 0.0:
                continue
            score = clamp(0.82 * match_score + 0.18 * coverage)
            reasons = dict(item.reasons)
            reasons.update(
                {
                    "match_score": match_score,
                    "coverage": coverage,
                    "document_evidence_score": evidence_score,
                    "document_label_score": label_score,
                }
            )
            candidates.append(RankedNode(node=display_node, score=score, reasons=reasons))

        candidates.sort(
            key=lambda item: (
                item.score,
                float(item.reasons.get("coverage", 0.0)),
                float(item.reasons.get("document_label_score", 0.0)),
                len(self._node_label(item.node)),
                self._location_summary(item.node) or "",
            ),
            reverse=True,
        )
        if not candidates:
            return []

        top_score = candidates[0].score
        top_path = self._node_relative_path(candidates[0].node) or ""
        selected: list[RankedNode] = []
        seen_evidence: set[tuple[str, str]] = set()
        per_path: dict[str, int] = {}
        for item in candidates:
            path = self._node_relative_path(item.node) or ""
            relative_floor = top_score * (0.40 if path and path == top_path else 0.60)
            if item.score < max(0.08, relative_floor):
                continue
            text_key = " ".join(str(item.node.text or item.node.label or "").casefold().split())
            evidence_key = (self._location_summary(item.node) or path, text_key)
            if evidence_key in seen_evidence or per_path.get(path, 0) >= 3:
                continue
            seen_evidence.add(evidence_key)
            per_path[path] = per_path.get(path, 0) + 1
            selected.append(item)
            if len(selected) >= limit:
                break
        return selected


    def _has_source_relation(self, node_id: str) -> bool:
        for edge, _ in self.store.neighbors(node_id, direction="both", edge_types=SOURCE_EDGE_TYPES, limit=20):
            if edge.type in SOURCE_EDGE_TYPES:
                return True
        return False

    def _collect_sources(
        self,
        nodes: OrderedDict[str, MemoryNode],
        edges: OrderedDict[str, MemoryEdge],
        *,
        query: MemoryQuery,
        seed_nodes: OrderedDict[str, MemoryNode],
        limit: int,
    ) -> OrderedDict[str, MemoryNode]:
        sources: OrderedDict[str, MemoryNode] = OrderedDict()
        query_tokens = set(_expanded_tokens(query.text))
        for node in nodes.values():
            if node.type in SOURCE_NODE_TYPES:
                if not self._source_is_query_relevant(node, query=query, edges=edges, seed_nodes=seed_nodes, query_tokens=query_tokens):
                    continue
                sources.setdefault(node.id, node)
                if len(sources) >= limit:
                    return sources
        for node_id in list(nodes):
            for edge, neighbor in self.store.neighbors(node_id, direction="both", edge_types=SOURCE_EDGE_TYPES, limit=40):
                if edge.type in TECHNICAL_EDGE_TYPES or neighbor.type in TECHNICAL_NODE_TYPES:
                    continue
                if not query.include_archived and neighbor.status in INACTIVE_STATUSES:
                    continue
                if neighbor.type not in SOURCE_NODE_TYPES:
                    continue
                if not self._source_is_query_relevant(neighbor, query=query, edges=OrderedDict([(edge.id, edge)]), seed_nodes=seed_nodes, query_tokens=query_tokens):
                    continue
                edges.setdefault(edge.id, edge)
                sources.setdefault(neighbor.id, neighbor)
                if len(sources) >= limit:
                    return sources
        return sources

    def _filter_generic_context_nodes(
        self,
        nodes: OrderedDict[str, MemoryNode],
        edges: OrderedDict[str, MemoryEdge],
        *,
        query_text: str,
        seed_nodes: set[str],
        sources: set[str],
    ) -> tuple[OrderedDict[str, MemoryNode], OrderedDict[str, MemoryEdge], list[str]]:
        query_tokens = set(_expanded_tokens(query_text))
        degree: dict[str, int] = {node_id: 0 for node_id in nodes}
        for edge in edges.values():
            if edge.from_id in degree:
                degree[edge.from_id] += 1
            if edge.to_id in degree:
                degree[edge.to_id] += 1

        seed_adjacent_semantic: set[str] = set()
        for edge in edges.values():
            if edge.type in TECHNICAL_EDGE_TYPES or edge.type == "ABOUT":
                continue
            if edge.from_id in seed_nodes:
                seed_adjacent_semantic.add(edge.to_id)
            if edge.to_id in seed_nodes:
                seed_adjacent_semantic.add(edge.from_id)

        filtered: list[str] = []
        kept: OrderedDict[str, MemoryNode] = OrderedDict()
        for node_id, node in nodes.items():
            if node.type in TECHNICAL_NODE_TYPES:
                filtered.append(node_id)
                continue
            if node_id in seed_nodes or node_id in sources:
                kept[node_id] = node
                continue
            direct = self._direct_relevance_score(node, query_text, query_tokens=query_tokens)
            if node.type in SOURCE_NODE_TYPES and direct < 0.85:
                filtered.append(node_id)
                continue
            if self._is_generic_context_node(node, degree.get(node_id, 0)):
                filtered.append(node_id)
                continue
            if node.type in {"Topic", "Entity"} and direct <= 0.0 and node_id not in seed_adjacent_semantic:
                filtered.append(node_id)
                continue
            kept[node_id] = node
        kept_edges = OrderedDict((edge_id, edge) for edge_id, edge in edges.items() if edge.from_id in kept and edge.to_id in kept)
        return kept, kept_edges, filtered

    @staticmethod
    def _is_explicit_code_context(node_types: Sequence[str] | None = None) -> bool:
        if node_types:
            requested = {node_type for node_type in node_types}
            if requested and requested <= CODE_CONTEXT_NODE_TYPES:
                return True
        return False

    @classmethod
    def _is_code_context_node(cls, node: MemoryNode) -> bool:
        if node.type in CODE_CONTEXT_EXCLUDED_NODE_TYPES or node.type in TECHNICAL_NODE_TYPES:
            return False
        if node.type not in CODE_CONTEXT_NODE_TYPES:
            return False
        path = cls._node_relative_path(node)
        if path and cls._is_generated_context_path(path):
            return False
        if node.type == "SourceFragment":
            return bool(path and cls._is_code_source_path(path))
        return True

    @staticmethod
    def _is_generated_context_path(path: str) -> bool:
        value = path.replace("\\", "/")
        return any(part in value for part in (".egg-info/", "__pycache__/", ".reql/", ".git/"))

    @staticmethod
    def _is_code_source_path(path: str) -> bool:
        value = path.replace("\\", "/").lstrip("/").casefold()
        if value.startswith(("src/", "tests/")):
            return True
        return GeneralContextProjectionMixin._is_application_surface_path(value)

    @staticmethod
    def _is_application_surface_path(path: str) -> bool:
        value = path.replace("\\", "/").lstrip("/").casefold()
        if value.startswith(
            (
                "app/views/",
                "app/templates/",
                "public/assets/",
                "public/css/",
                "public/js/",
                "resources/views/",
                "resources/css/",
                "resources/js/",
                "templates/",
                "views/",
                "assets/",
                "static/",
            )
        ):
            return True
        return any(part in value for part in ("/views/", "/templates/", "/public/assets/", "/static/"))

    @staticmethod
    def _is_test_context_path(path: str) -> bool:
        value = path.replace("\\", "/").lstrip("/")
        name = value.rsplit("/", 1)[-1]
        return value.startswith("tests/") or name.startswith("test_") or name.endswith("_test.py")

    def _is_generic_context_node(self, node: MemoryNode, local_degree: int) -> bool:
        text = (node.canonical_key or node.label or node.text or "").strip().casefold()
        if node.type in TECHNICAL_NODE_TYPES:
            return True
        if node.type in SOURCE_NODE_TYPES:
            return False
        if node.type in {"Topic", "Entity"} and local_degree <= 1 and len(text) <= 2:
            return True
        if node.type in {"Topic", "Entity"} and local_degree == 0:
            return True
        return False

    def _is_relevant_memory_node(self, node: MemoryNode, query_text: str, *, query_tokens: set[str] | None = None) -> bool:
        if node.type in TECHNICAL_NODE_TYPES:
            return False
        query_tokens = query_tokens if query_tokens is not None else set(_expanded_tokens(query_text))
        direct = self._direct_relevance_score(node, query_text, query_tokens=query_tokens)
        if node.type in {"Topic", "Entity", "Fact"}:
            return direct >= 0.65
        return direct >= 0.50

    def _source_is_query_relevant(
        self,
        source: MemoryNode,
        *,
        query: MemoryQuery,
        edges: OrderedDict[str, MemoryEdge],
        seed_nodes: OrderedDict[str, MemoryNode],
        query_tokens: set[str] | None = None,
    ) -> bool:
        if source.type in TECHNICAL_NODE_TYPES:
            return False
        query_tokens = query_tokens if query_tokens is not None else set(_expanded_tokens(query.text))
        if self._direct_relevance_score(source, query.text, query_tokens=query_tokens) >= 0.85:
            return True
        for edge in edges.values():
            if edge.type in TECHNICAL_EDGE_TYPES:
                continue
            if source.id != edge.from_id and source.id != edge.to_id:
                continue
            other_id = edge.to_id if edge.from_id == source.id else edge.from_id
            seed = seed_nodes.get(other_id)
            if seed is None or seed.type not in GRAPH_SEED_NODE_TYPES or seed.type in TECHNICAL_NODE_TYPES:
                continue
            if edge.type == "EVIDENCED_BY" and self._is_code_context_node(seed):
                return True
            seed_direct = self._direct_relevance_score(seed, query.text, query_tokens=query_tokens)
            if seed_direct >= 0.65 and not self._is_generic_context_node(seed, 2):
                return True
        return False


    def _agent_ranked_payload(self, item: RankedNode, *, max_text_chars: int = 220) -> dict[str, Any]:
        payload = self._ranked_payload(item)
        payload["label"] = self._compact_text(self._node_label(item.node), max_chars=140)
        payload["text"] = self._compact_text(item.node.text or "", max_chars=max_text_chars)
        payload["location"] = self._location_summary(item.node)
        return payload

    def _agent_node_lines(self, item: RankedNode, *, max_text_chars: int) -> list[str]:
        node = item.node
        label = self._compact_text(self._node_label(node), max_chars=140)
        parts = [f"- ({item.score:.2f}) `{node.id}` [{node.type}] {label}"]
        location = self._location_summary(node)
        if location:
            parts[0] += f" @ {location}"
        text = self._compact_text(node.text or "", max_chars=max_text_chars)
        if text and text != label:
            parts.append(f"  text: {text}")
        return parts

    def _agent_source_payloads(self, subgraph: MemorySubgraph, *, max_items: int, query_text: str | None = None) -> list[dict[str, Any]]:
        candidates: OrderedDict[str, MemoryNode] = OrderedDict()
        query_tokens = set(_expanded_tokens(query_text or ""))
        for item in subgraph.ranked_nodes:
            if item.node.type in SOURCE_NODE_TYPES:
                candidates.setdefault(item.node.id, item.node)
        for node in subgraph.nodes:
            if node.type in SOURCE_NODE_TYPES:
                candidates.setdefault(node.id, node)
        payloads: list[dict[str, Any]] = []
        seen_sources: set[str] = set()
        limit = min(max_items, 20)
        has_non_test_source = False
        if query_tokens:
            for node in candidates.values():
                text = self._compact_text(node.text or node.label or node.canonical_key or node.id, max_chars=260)
                path = self._node_relative_path(node) or ""
                if query_tokens & set(_expanded_tokens(text)) and not self._is_test_context_path(path):
                    has_non_test_source = True
                    break
        for node in candidates.values():
            text = self._compact_text(node.text or node.label or node.canonical_key or node.id, max_chars=260)
            if query_tokens and not (query_tokens & set(_expanded_tokens(text))):
                continue
            path = self._node_relative_path(node) or ""
            if has_non_test_source and self._is_test_context_path(path):
                continue
            location = self._location_summary(node)
            dedupe_key = f"{location or ''}|{' '.join(text.casefold().split())}"
            if dedupe_key in seen_sources:
                continue
            seen_sources.add(dedupe_key)
            payloads.append(
                {
                    "id": node.id,
                    "type": node.type,
                    "label": self._compact_text(self._node_label(node), max_chars=140),
                    "text": text,
                    "location": location,
                }
            )
            if len(payloads) >= limit:
                break
        return payloads

    def _general_result_payloads(
        self,
        ranked_items: list[dict[str, Any]],
        source_items: list[dict[str, Any]],
        *,
        max_items: int,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        text_index: dict[str, dict[str, Any]] = {}
        id_index: set[str] = set()
        limit = min(max_items, 20)

        def text_key(item: dict[str, Any]) -> str:
            text = str(item.get("text") or item.get("label") or "")
            return " ".join(text.casefold().split())

        def location_path(item: dict[str, Any]) -> str:
            location = str(item.get("location") or "")
            return location.split(":", 1)[0].casefold()

        def overlap_target(item: dict[str, Any]) -> dict[str, Any] | None:
            item_path = location_path(item)
            if not item_path:
                return None
            item_tokens = set(tokenize(str(item.get("text") or item.get("label") or "")))
            if not item_tokens:
                return None
            for result in results:
                if location_path(result) != item_path:
                    continue
                result_tokens = set(tokenize(str(result.get("text") or result.get("label") or "")))
                if not result_tokens:
                    continue
                overlap = item_tokens & result_tokens
                if overlap and result.get("kind") == "match":
                    return result
                if len(overlap) >= 3 and len(overlap) / max(1, min(len(item_tokens), len(result_tokens))) >= 0.45:
                    return result
            return None

        def merge_source(existing: dict[str, Any], item: dict[str, Any]) -> None:
            source_id = str(item.get("id") or "")
            location = str(item.get("location") or "")
            if source_id and source_id not in existing["source_ids"]:
                existing["source_ids"].append(source_id)
            if location and location not in existing["source_locations"]:
                existing["source_locations"].append(location)
            if not existing.get("location") and location:
                existing["location"] = location

        for item in ranked_items:
            if item.get("type") in SOURCE_NODE_TYPES:
                existing = overlap_target(item)
                if existing is not None:
                    merge_source(existing, item)
                    continue
            if len(results) >= limit:
                break
            key = text_key(item)
            result = dict(item)
            result["kind"] = "match"
            result["source_ids"] = []
            result["source_locations"] = []
            results.append(result)
            id_index.add(str(result.get("id") or ""))
            if key:
                text_index.setdefault(key, result)

        for item in source_items:
            key = text_key(item)
            existing = text_index.get(key) if key else None
            if existing is None:
                existing = overlap_target(item)
            if existing is not None:
                merge_source(existing, item)
                continue
            if len(results) >= limit:
                break
            item_id = str(item.get("id") or "")
            if item_id in id_index:
                continue
            result = dict(item)
            result["kind"] = "source"
            result["score"] = None
            result["source_ids"] = []
            result["source_locations"] = []
            results.append(result)
            id_index.add(item_id)
            if key:
                text_index.setdefault(key, result)
        return results

    def _agent_edge_lines(
        self,
        subgraph: MemorySubgraph,
        *,
        max_items: int,
        hide_raw_event_links: bool = False,
        hide_test_code_links: bool = False,
    ) -> list[str]:
        nodes: dict[str, MemoryNode] = {item.node.id: item.node for item in subgraph.ranked_nodes}
        nodes.update({node.id: node for node in subgraph.nodes})
        lines: list[str] = []
        seen: set[str] = set()
        limit = min(max_items, 8)
        for edge in subgraph.edges:
            if edge.id in seen or edge.type in TECHNICAL_EDGE_TYPES or edge.type in {"MENTIONS", "ABOUT"}:
                continue
            left = nodes.get(edge.from_id)
            right = nodes.get(edge.to_id)
            if left is None or right is None:
                continue
            if hide_raw_event_links and (left.type == "RawEvent" or right.type == "RawEvent"):
                continue
            if hide_raw_event_links and edge.type == "DERIVED_FROM" and (left.type in SOURCE_NODE_TYPES or right.type in SOURCE_NODE_TYPES):
                continue
            if hide_test_code_links and (self._is_test_context_path(self._node_relative_path(left) or "") or self._is_test_context_path(self._node_relative_path(right) or "")):
                continue
            seen.add(edge.id)
            left_label = self._compact_text(self._node_label(left), max_chars=90)
            right_label = self._compact_text(self._node_label(right), max_chars=90)
            location = self._location_summary(edge)
            suffix = f" @ {location}" if location else ""
            lines.append(f"- `{edge.id}` {left_label} --{edge.type}--> {right_label}{suffix}")
            if len(lines) >= limit:
                break
        return lines

    def _agent_follow_up_lines(self, subgraph: MemorySubgraph, *, max_items: int) -> list[str]:
        return self._render_followups(self._agent_follow_up_payload(subgraph, max_items=max_items))

    def _agent_follow_up_payload(
        self,
        subgraph: MemorySubgraph,
        *,
        max_items: int,
        ranked_items: Sequence[RankedNode] | None = None,
    ) -> list[dict[str, str]]:
        followup_items = list(ranked_items) if ranked_items is not None else subgraph.ranked_nodes
        ids = [item.node.id for item in followup_items[: min(3, max_items)]]
        query = self._reql_string(subgraph.query.text)
        followups: list[dict[str, str]] = []
        if ids:
            followups.append(
                {
                    "label": "Inspect top node",
                    "command": f"reql inspect --node-id {ids[0]} --json",
                    "purpose": "top node provenance and neighbors",
                }
            )
        non_source_id = next((item.node.id for item in followup_items if item.node.type not in SOURCE_NODE_TYPES), None)
        if ids and non_source_id and non_source_id != ids[0]:
            followups.append(
                {
                    "label": "Inspect best non-source node",
                    "command": f"reql inspect --node-id {non_source_id} --json",
                    "purpose": "best non-source node provenance and neighbors",
                }
            )
        retrieve_statement = f"RETRIEVE {query} LIMIT {min(max_items, 8)} RETURN id,type,text,score,source_for,relation,direction,relative_path,line_start"
        followups.append(
            {
                "label": "Retrieve source rows",
                "command": f"reql query {self._shell_string(retrieve_statement)}",
                "purpose": "compact source/location rows",
            }
        )
        followups.append(
            {
                "label": "Expand graph context",
                "command": f"reql query_graph --query {query} --max-depth {subgraph.query.max_depth} --json",
                "purpose": "expanded graph context",
            }
        )
        if len(ids) > 1:
            id_list = ", ".join(self._reql_string(node_id) for node_id in ids)
            compare_statement = f"FIND nodes WHERE id IN [{id_list}] RETURN id,type,label,text"
            followups.append(
                {
                    "label": "Compare top ids",
                    "command": f"reql query {self._shell_string(compare_statement)}",
                    "purpose": "comparison of close matches",
                }
            )
        return followups

    @staticmethod
    def _node_relative_path(node: MemoryNode) -> str | None:
        props = dict(node.properties)
        metadata = props.get("metadata")
        if isinstance(metadata, dict):
            for key in ("relative_path", "source_file", "path", "source_path"):
                if key not in props and metadata.get(key) is not None:
                    props[key] = metadata.get(key)
        path = props.get("relative_path") or props.get("source_file") or props.get("path") or props.get("source_path")
        if path is None:
            return None
        value = str(path).replace("\\", "/")
        if not value or "://" in value:
            return None
        marker = "/src/"
        if marker in value:
            return "src/" + value.rsplit(marker, 1)[1]
        marker = "/tests/"
        if marker in value:
            return "tests/" + value.rsplit(marker, 1)[1]
        return value

    @staticmethod
    def _line_span(item: MemoryNode | MemoryEdge) -> tuple[int | None, int | None]:
        props = dict(item.properties)
        metadata = props.get("metadata")
        if isinstance(metadata, dict):
            for key in ("line_start", "start_line", "line_end", "end_line"):
                if key not in props and metadata.get(key) is not None:
                    props[key] = metadata.get(key)
        start = props.get("line_start", props.get("start_line"))
        end = props.get("line_end", props.get("end_line"))
        try:
            parsed_start = int(start) if start is not None else None
        except (TypeError, ValueError):
            parsed_start = None
        try:
            parsed_end = int(end) if end is not None else parsed_start
        except (TypeError, ValueError):
            parsed_end = parsed_start
        return parsed_start, parsed_end

    @classmethod
    def _location_summary(cls, item: MemoryNode | MemoryEdge) -> str | None:
        props = dict(item.properties)
        metadata = props.get("metadata")
        if isinstance(metadata, dict):
            for key in ("source_path", "path", "relative_path", "source_file", "source_url", "url"):
                if key not in props and metadata.get(key) is not None:
                    props[key] = metadata.get(key)
            for key in ("line_start", "start_line", "line_end", "end_line"):
                if key not in props and metadata.get(key) is not None:
                    props[key] = metadata.get(key)
        path = props.get("relative_path") or props.get("source_file") or props.get("path") or props.get("source_path") or props.get("source_url") or props.get("url")
        if not path:
            return None
        start = props.get("line_start", props.get("start_line"))
        end = props.get("line_end", props.get("end_line"))
        if start is None and end is None:
            return str(path)
        if end is None or end == start:
            return f"{path}:{start}"
        return f"{path}:{start}-{end}"

    @staticmethod
    def _reql_string(value: str) -> str:
        escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    @staticmethod
    def _shell_string(value: str) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    def _source_context_payload(
        self,
        node: MemoryNode,
        *,
        nodes: OrderedDict[str, MemoryNode] | dict[str, MemoryNode] | None = None,
        edges: Any = (),
    ) -> dict[str, Any]:
        return {
            "id": node.id,
            "type": node.type,
            "label": node.label,
            "text": self._compact_text(node.text or node.label or node.canonical_key or "", max_chars=600),
            "source_for": self._source_relation_payload(node, nodes or {}, edges),
            "properties": dict(node.properties),
        }

    def _source_relation_payload(
        self,
        source: MemoryNode,
        nodes: OrderedDict[str, MemoryNode] | dict[str, MemoryNode],
        edges: Any,
    ) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = []
        for edge in edges:
            if source.id != edge.from_id and source.id != edge.to_id:
                continue
            other_id = edge.to_id if edge.from_id == source.id else edge.from_id
            other = nodes.get(other_id)
            if other is None:
                continue
            refs.append(
                {
                    "node_id": other.id,
                    "node_type": other.type,
                    "node_label": self._node_label(other),
                    "relation": edge.type,
                    "direction": "outgoing" if edge.from_id == source.id else "incoming",
                    "edge_id": edge.id,
                }
            )
        return refs

    def _source_relation_refs(
        self,
        source: MemoryNode,
        nodes: dict[str, MemoryNode],
        edges: list[MemoryEdge],
        *,
        limit: int,
    ) -> list[str]:
        refs: list[str] = []
        for item in self._source_relation_payload(source, nodes, edges):
            direction = "outgoing" if item["direction"] == "outgoing" else "incoming"
            refs.append(f"{item['relation']} {direction} {item['node_label']}")
            if len(refs) >= limit:
                break
        return refs

    @staticmethod
    def _node_label(node: MemoryNode) -> str:
        return node.label or node.text or node.canonical_key or node.id

    @staticmethod
    def _compact_text(text: str, *, max_chars: int = 320) -> str:
        compact = " ".join(str(text).split())
        if len(compact) <= max_chars:
            return compact
        return compact[: max_chars - 3].rstrip() + "..."
