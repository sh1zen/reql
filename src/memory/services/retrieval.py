"""Deterministic lexical + bounded graph retrieval."""
from __future__ import annotations

from collections import OrderedDict
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
import re
from typing import Any, Sequence

from ..diagnostics import PerformanceLogger
from ..domain.constants import INACTIVE_STATUSES
from ..extraction.deterministic import DeterministicExtractor
from ..domain.ids import stable_id
from ..domain.models import MemoryEdge, MemoryNode, MemoryQuery, MemorySubgraph, RankedNode
from ..extraction.normalization import canonicalize, clamp, token_signal_score, tokenize
from ..domain.timeutils import utcnow_iso
from ..storage.extractor import SemanticExtractor
from ..storage.graph_store import GraphStore

TECHNICAL_NODE_TYPES = {"RetrievalTrace", "System", "Session", "Debug", "Log", "Comment", "Docstring", "Import"}
GRAPH_SEED_NODE_TYPES = {"Topic", "Entity", "Fact", "File", "SourceArtifact", "Module", "Function", "Class", "Interface", "Method", "Variable", "Endpoint", "Schema", "StaticAnalysisFinding"}
CODE_CONTEXT_NODE_TYPES = {
    "SourceArtifact",
    "SourceFragment",
    "File",
    "Module",
    "Function",
    "Class",
    "Interface",
    "Method",
    "Variable",
    "Endpoint",
    "Schema",
    "Config",
    "Test",
    "Dependency",
    "StaticAnalysisFinding",
}
CODE_CONTEXT_EXCLUDED_NODE_TYPES = {
    "Entity",
    "Fact",
    "Topic",
}
SEMANTIC_EDGE_TYPES = {
    "ABOUT",
    "HAS_TOPIC",
    "MENTIONS",
    "DERIVED_FROM",
    "RELATED_TO",
    "IS_A",
    "LIKES",
    "SUPPORTS",
    "EXPRESSES",
    "EXPLAINS",
    "EVIDENCED_BY",
    "SUPPORTED_BY",
    "PART_OF",
    "INSTANCE_OF",
    "CONFIRMS",
    "CORRECTS",
    "SUPERSEDES",
    "UPDATED_BY",
    "TRACKS",
}
TECHNICAL_EDGE_TYPES = {
    "GENERATED_BY_QUERY",
    "USED_IN_CONTEXT",
    "COMPILED_IN",
    "AFFECTED_BY_DELTA",
    "ASSOCIATED_WITH",
}
CODE_CONTEXT_EDGE_TYPES = {
    "CALLS",
    "CONTAINS",
    "DEFINES",
    "DEPENDS_ON",
    "EVIDENCED_BY",
    "HANDLES_ROUTE",
    "HAS_FINDING",
    "HAS_SECTION",
    "IMPORTS",
    "IMPLEMENTS",
    "IMPORTS_FROM",
    "INHERITS",
    "INSTANTIATES",
    "OVERRIDES",
    "METHOD",
    "RAISES",
    "READS",
    "REFERENCES",
    "RE_EXPORTS",
    "RETURNS",
    "WRITES",
    "WRAPS",
}
DEFAULT_CONTEXT_EDGE_TYPES = SEMANTIC_EDGE_TYPES | CODE_CONTEXT_EDGE_TYPES
SOURCE_NODE_TYPES = {"SourceFragment", "DocumentFragment"}
SOURCE_EDGE_TYPES = {"EVIDENCED_BY", "DERIVED_FROM", "SUPPORTED_BY", "CONTAINS_FRAGMENT", "HAS_SECTION", "HAS_DOCSTRING", "HAS_COMMENT"}
QUERY_EXPLORE_VIEWS = {"owners", "callers", "public_surface", "serialization_paths", "docs_mentions", "structural_duplicates", "code"}
QUERY_EXPLORE_DEFAULT_VIEWS = ("owners", "callers", "public_surface", "serialization_paths", "docs_mentions", "code")
QUERY_EXPLORE_ALL_VIEWS = (*QUERY_EXPLORE_DEFAULT_VIEWS[:-1], "structural_duplicates", "code")
QUERY_EXPLORE_EDGE_TYPES = CODE_CONTEXT_EDGE_TYPES | SOURCE_EDGE_TYPES | {"DECORATED_BY", "HAS_CODE_BLOCK", "HAS_COMMENT", "HAS_DOCSTRING", "TESTS"}
OWNER_EDGE_TYPES = {"CONTAINS", "DEFINES", "EVIDENCED_BY", "HAS_FINDING", "METHOD"}
CALLER_EDGE_TYPES = {"CALLS", "INSTANTIATES"}
PUBLIC_SURFACE_EDGE_TYPES = {"HANDLES_ROUTE", "IMPLEMENTS", "IMPORTS", "IMPORTS_FROM", "RE_EXPORTS"}
SERIALIZATION_EDGE_TYPES = {"READS", "WRITES", "RETURNS", "RAISES", "REFERENCES", "EVIDENCED_BY", "DEPENDS_ON", "IMPORTS_FROM"}
QUERY_CONTEXT_MODES = {"informative", "cleanup"}
QUERY_CONTEXT_SCOPES = {"code", "docs", "test"}
QUERY_CONTEXT_MIN_CONFIDENCE_SCORE = 0.25
QUERY_CONTEXT_MAX_RENDERED_FILES = 8
QUERY_CONTEXT_MAX_RENDERED_TEST_FILES = 3
STRUCTURED_SEARCH_FIELDS = (
    "name",
    "qualified_name",
    "symbol_name",
    "module",
    "target",
    "relative_path",
    "source_file",
    "language",
    "kind",
    "args",
    "bases",
    "returns",
    "semantic_roles",
    "wrapper_targets",
    "overrides",
    "re_exports",
)
CODE_CONTEXT_GENERIC_QUERY_TOKENS = {
    "agent",
    "base",
    "bug",
    "change",
    "changes",
    "code",
    "context",
    "edit",
    "edits",
    "fix",
    "fixes",
    "function",
    "functions",
    "implementation",
    "implement",
    "issue",
    "logic",
    "noise",
    "problem",
    "query",
    "request",
    "task",
    "unrelated",
    "workflow",
}
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_CANONICAL_TOKEN_RE = re.compile(r"[a-z0-9_][a-z0-9_'-]{1,}")


@dataclass(slots=True)
class _QueryProfile:
    text: str
    canonical: str
    tokens: set[str]
    informative_tokens: set[str]
    ordered_tokens: tuple[str, ...]
    phrase_terms: set[str]


@dataclass(slots=True)
class _PathCandidate:
    node: MemoryNode
    score: float
    match_score: float
    coverage: float
    path_score: float
    type_bonus: float
    seed_score: float
    depth_penalty: float
    edge_ids: list[str] = field(default_factory=list)


@lru_cache(maxsize=32768)
def _expanded_tokens(value: str) -> tuple[str, ...]:
    tokens: list[str] = []
    seen: set[str] = set()
    for candidate in (value, _identifier_expanded_text(value)):
        for token in tokenize(candidate):
            for variant in _token_variants(token):
                if variant not in seen:
                    seen.add(variant)
                    tokens.append(variant)
    return tuple(tokens)


def _code_context_query_tokens(value: str) -> set[str]:
    tokens = set(_expanded_tokens(value))
    filtered = {token for token in tokens if token not in CODE_CONTEXT_GENERIC_QUERY_TOKENS}
    return filtered or tokens


def _identifier_expanded_text(value: str) -> str:
    separated = str(value).replace("\\", " ").replace("/", " ").replace(".", " ")
    separated = separated.replace("_", " ").replace("-", " ").replace(":", " ")
    return _CAMEL_BOUNDARY_RE.sub(" ", separated)


def _token_variants(token: str) -> tuple[str, ...]:
    variants = [token]
    singular = _singular_token(token)
    if singular and singular != token:
        variants.append(singular)
    return tuple(variants)


def _singular_token(token: str) -> str | None:
    if len(token) <= 3:
        return None
    if token.endswith("ies") and len(token) > 4:
        return f"{token[:-3]}y"
    if token.endswith("sses"):
        return None
    if token.endswith("ses") and len(token) > 4:
        return token[:-2]
    if token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return None


def _canonical_token_overlap(value: str, query_tokens: set[str]) -> set[str]:
    """Return expanded query-token matches from already-canonical search text."""
    if not value or not query_tokens:
        return set()
    overlap: set[str] = set()
    for match in _CANONICAL_TOKEN_RE.finditer(value):
        token = match.group(0).strip("_-")
        if len(token) < 2:
            continue
        for variant in _token_variants(token):
            if variant in query_tokens:
                overlap.add(variant)
    return overlap


def _raw_query_token_overlap(parts: Sequence[str], query_tokens: set[str]) -> set[str] | None:
    """Return a conservative cheap overlap, or ``None`` when Unicode needs normalization."""
    raw = " ".join(parts)
    if not raw.isascii():
        return None
    folded = raw.casefold()
    overlap: set[str] = set()
    for token in query_tokens:
        needles = (token, f"{token[:-1]}ie") if token.endswith("y") and len(token) > 2 else (token,)
        if any(needle in folded for needle in needles):
            overlap.add(token)
    return overlap


class RetrievalEngine:
    """Retrieves a relevant subgraph without LLM calls.

    Seed nodes are found through deterministic extraction and lexical search.
    Relevance is then ranked by direct node match plus bounded graph paths that
    increase query-token coverage. The free-search path intentionally avoids
    historical memory signals so repository context stays deterministic and
    code-oriented.
    """

    def __init__(self, store: GraphStore, extractor: SemanticExtractor | None = None, *, profile_logger: PerformanceLogger | None = None) -> None:
        self.store = store
        self.extractor = extractor or DeterministicExtractor()
        self.profile_logger = profile_logger

    def retrieve(self, query: MemoryQuery) -> MemorySubgraph:
        profile = self.profile_logger
        with (profile.span("retrieval.total", top_k=query.top_k, max_depth=query.max_depth) if profile else nullcontext()):
            return self._retrieve(query, profile=profile)

    @staticmethod
    def _is_graph_layer_node(node: MemoryNode) -> bool:
        return node.type not in TECHNICAL_NODE_TYPES

    def _retrieve(self, query: MemoryQuery, *, profile: PerformanceLogger | None) -> MemorySubgraph:
        with (profile.span("retrieval.extract") if profile else nullcontext()):
            extraction = self.extractor.extract(query.text)
        seed_scores: OrderedDict[str, float] = OrderedDict()
        with (profile.span("retrieval.tokenize") if profile else nullcontext()):
            query_profile = self._query_profile(query.text)
        query_scopes = self._normalize_query_context_scopes(query.context_scopes)
        code_context = self._is_explicit_code_context(query.node_types) or bool(query_scopes and query_scopes <= {"code", "test"})
        traversal_edge_types = query.edge_types if query.edge_types is not None else (CODE_CONTEXT_EDGE_TYPES if code_context else DEFAULT_CONTEXT_EDGE_TYPES)
        lexical_node_types = query.node_types
        if code_context and lexical_node_types is None:
            lexical_node_types = tuple(sorted(CODE_CONTEXT_NODE_TYPES))

        # 1) canonical topic/entity matches.
        with (profile.span("retrieval.canonical_seed", topics=len(extraction.topics), entities=len(extraction.entities)) if profile else nullcontext()):
            for topic, score in extraction.topics:
                node = self.store.get_node_by_key("Topic", canonicalize(topic))
                if node:
                    if query_scopes and not self._node_matches_query_context_scope(node, query_scopes):
                        continue
                    seed_scores[node.id] = max(
                        seed_scores.get(node.id, 0.0),
                        0.55 + 0.40 * score,
                        self._direct_relevance_score(node, query.text, query_tokens=query_profile.tokens),
                    )
            for entity, _, score in extraction.entities:
                node = self.store.get_node_by_key("Entity", canonicalize(entity))
                if node:
                    if query_scopes and not self._node_matches_query_context_scope(node, query_scopes):
                        continue
                    seed_scores[node.id] = max(
                        seed_scores.get(node.id, 0.0),
                        0.60 + 0.35 * score,
                        self._direct_relevance_score(node, query.text, query_tokens=query_profile.tokens),
                    )

        # 2) lexical search across the graph.
        with (profile.span("retrieval.lexical_search", top_k=max(query.top_k * 3, 30)) if profile else nullcontext()):
            lexical_matches = (
                self._scoped_lexical_search(
                    query,
                    query_profile,
                    lexical_node_types=lexical_node_types,
                    scopes=query_scopes,
                    top_k=max(query.top_k * 8, 120),
                )
                if query_scopes
                else self.store.lexical_search(
                    query.text,
                    top_k=max(query.top_k * 3, 30),
                    node_types=lexical_node_types,
                    include_archived=query.include_archived,
                )
            )
            for node, score in lexical_matches:
                if node.type in TECHNICAL_NODE_TYPES:
                    continue
                if code_context and not self._is_code_context_node(node):
                    continue
                if query_scopes and not self._node_matches_query_context_scope(node, query_scopes):
                    continue
                metrics = self._node_match_metrics(node, query_profile)
                if self._is_weak_multiterm_match(
                    node,
                    query_tokens=query_profile.informative_tokens,
                    direct_relevance=metrics["match_score"],
                    overlap_count=int(metrics.get("overlap_count", 0.0)),
                    has_strong_identifier_overlap=bool(metrics.get("strong_identifier_overlap", 0.0)),
                ):
                    continue
                adjusted_score = max(score, metrics["match_score"])
                seed_scores[node.id] = max(seed_scores.get(node.id, 0.0), adjusted_score)

        sorted_seed_scores = sorted(seed_scores.items(), key=lambda item: item[1], reverse=True)
        seed_node_ids = self._pick_seed_node_ids(sorted_seed_scores, max_k=max(query.top_k * 2, 20))
        with (profile.span("retrieval.expand", seed_nodes=len(seed_node_ids), max_depth=query.max_depth) if profile else nullcontext()):
            candidates, candidate_edges = self._expand_and_rank_candidates(
                seed_node_ids,
                seed_scores,
                query,
                query_profile,
                edge_types=traversal_edge_types,
                code_context=code_context,
            )

        ranked: list[RankedNode] = []
        with (profile.span("retrieval.rank", candidate_nodes=len(candidates)) if profile else nullcontext()):
            for candidate in candidates.values():
                if candidate.score <= 0.0 and candidate.match_score <= 0.0:
                    continue
                ranked.append(
                    RankedNode(
                        node=candidate.node,
                        score=candidate.score,
                        reasons={
                            "match_score": candidate.match_score,
                            "coverage": candidate.coverage,
                            "path_score": candidate.path_score,
                            "type_bonus": candidate.type_bonus,
                            "seed_score": candidate.seed_score,
                            "depth_penalty": candidate.depth_penalty,
                        },
                    )
                )
        ranked.sort(key=lambda item: item.score, reverse=True)
        ranked = ranked[: query.top_k]
        if query.store_trace:
            self.store.record_usage_event(
                query.text,
                [
                    {
                        "id": item.node.id,
                        "score": item.score,
                        "activation": item.reasons.get("path_score", 0.0),
                    }
                    for item in ranked
                    if item.node.type not in TECHNICAL_NODE_TYPES
                ],
            )

        # Context expansion: include immediate evidence/rationale/control edges.
        node_ids = [item.node.id for item in ranked]
        context_edges: OrderedDict[str, MemoryEdge] = OrderedDict()
        context_nodes: OrderedDict[str, MemoryNode] = OrderedDict((item.node.id, item.node) for item in ranked)
        expansion_edge_types = CODE_CONTEXT_EDGE_TYPES if code_context else {
            "SUPPORTS",
            "SUPERSEDES",
            "APPLIES_TO",
            "OVERRIDES",
            "ABOUT",
            "MENTIONS",
            "HAS_TOPIC",
            "PART_OF",
            "IS_A",
            "LIKES",
            "RELATED_TO",
            "DERIVED_FROM",
            "SUPPORTS",
            "SYNTHESIZES",
            "PROMOTED_TO",
            "EXPRESSES",
            "EXPLAINS",
            "EVIDENCED_BY",
            "UPDATED_BY",
            "TRACKS",
        }
        for edge in candidate_edges.values():
            if edge.type in TECHNICAL_EDGE_TYPES:
                continue
            if edge.type in expansion_edge_types and edge.from_id in context_nodes and edge.to_id in context_nodes:
                context_edges[edge.id] = edge
        for node_id in node_ids:
            neighbors = self.store.neighbors(
                node_id,

                direction="both",
                edge_types=traversal_edge_types,
                min_weight=0.25,
                limit=80,
            )
            for edge, neighbor in neighbors:
                if edge.type in TECHNICAL_EDGE_TYPES or neighbor.type in TECHNICAL_NODE_TYPES:
                    continue
                if code_context and not self._is_code_context_node(neighbor):
                    continue
                if query_scopes and not self._node_matches_query_context_scope(neighbor, query_scopes):
                    continue
                if not query.include_archived and neighbor.status in INACTIVE_STATUSES:
                    continue
                if edge.type in expansion_edge_types:
                    context_edges[edge.id] = edge
                    context_nodes.setdefault(neighbor.id, neighbor)

        trace_id: str | None = None
        if query.store_trace:
            trace_id = stable_id("retrieval", None, query.text, utcnow_iso())

        return MemorySubgraph(
            query=query,
            ranked_nodes=ranked,
            nodes=list(context_nodes.values()),
            edges=list(context_edges.values()),
            seed_node_ids=seed_node_ids,
            trace_id=trace_id,
        )

    def compose_context(
        self,
        subgraph: MemorySubgraph,
        *,
        max_items: int = 20,
        query_mode: str = "informative",
        query_scopes: Sequence[str] | None = None,
        include_risky: bool = False,
    ) -> str:
        """Render compact agent-ready context from a retrieved subgraph."""
        payload = self.query_context_payload(
            subgraph,
            max_items=max_items,
            query_mode=query_mode,
            query_scopes=query_scopes,
            include_risky=include_risky,
        )
        return self._render_query_context_payload(payload)

    def query_context_payload(
        self,
        subgraph: MemorySubgraph,
        *,
        max_items: int = 20,
        query_mode: str = "informative",
        query_scopes: Sequence[str] | None = None,
        include_risky: bool = False,
    ) -> dict[str, Any]:
        """Return compact structured agent context without duplicated rendered Markdown."""
        query_mode = self._normalize_query_context_mode(query_mode)
        scopes = self._normalize_query_context_scopes(query_scopes)
        subgraph = self._filter_query_context_subgraph(subgraph, scopes)
        explicit_code_scope = bool(scopes and scopes <= {"code", "test"})
        if explicit_code_scope or self._should_render_code_context(subgraph, max_items=max_items):
            payload = self._code_agent_context_payload(subgraph, query_mode=query_mode, max_items=max_items, include_risky=include_risky)
        else:
            payload = self._general_agent_context_payload(subgraph, query_mode=query_mode, max_items=max_items, include_risky=include_risky)
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
        include_risky: bool = False,
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
        cleanup_candidates = self._filter_cleanup_candidate_payloads(cleanup_payloads, include_risky=include_risky)[: min(max_items, 5)]
        return {
            "kind": "general",
            "query": subgraph.query.text,
            "query_mode": query_mode,
            "results": self._general_result_payloads(best_payloads, source_items, max_items=max_items),
            "cleanup_candidates": cleanup_candidates,
            "cleanup_filter": self._cleanup_filter_payload(
                include_risky=include_risky,
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
        include_risky: bool = False,
    ) -> str:
        """Return the compact deterministic context block for a query."""
        scoped_query = replace(query, context_scopes=set(query_scopes) if query_scopes else query.context_scopes)
        return self.compose_context(
            self.retrieve(scoped_query),
            max_items=max_items,
            query_mode=query_mode,
            query_scopes=query_scopes,
            include_risky=include_risky,
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
        return RetrievalEngine._is_application_surface_path(value)

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

    def _query_profile(self, query_text: str) -> _QueryProfile:
        ordered_tokens = tuple(_expanded_tokens(query_text))
        tokens = set(ordered_tokens)
        informative = {
            token
            for token in tokens
            if len(token) >= 3 or token_signal_score(token) >= 0.85
        }
        phrase_terms = {
            f"{a} {b}"
            for a, b in zip(ordered_tokens, ordered_tokens[1:])
            if token_signal_score(a) >= 0.5 and token_signal_score(b) >= 0.5
        }
        return _QueryProfile(
            text=query_text,
            canonical=canonicalize(query_text),
            tokens=tokens,
            informative_tokens=informative or tokens,
            ordered_tokens=ordered_tokens,
            phrase_terms=phrase_terms,
        )

    def _nodes_for_types(self, node_types: Sequence[str]) -> list[MemoryNode]:
        """Load only indexed node types while preserving ``all_nodes`` order."""
        nodes: dict[str, MemoryNode] = {}
        for node_type in sorted(set(node_types)):
            for node in self.store.find_nodes(type_=node_type, limit=2**63 - 1, clone=False):
                nodes.setdefault(node.id, node)
        return sorted(nodes.values(), key=lambda node: (node.created_at, node.id))

    def _scoped_lexical_search(
        self,
        query: MemoryQuery,
        query_profile: _QueryProfile,
        *,
        lexical_node_types: Sequence[str] | None,
        scopes: set[str],
        top_k: int,
    ) -> list[tuple[MemoryNode, float]]:
        allowed_types = set(lexical_node_types) if lexical_node_types is not None else None
        candidates = self._nodes_for_types(allowed_types) if allowed_types is not None else self.store.all_nodes()
        matches: list[tuple[MemoryNode, float]] = []
        for node in candidates:
            if allowed_types is not None and node.type not in allowed_types:
                continue
            if node.type in TECHNICAL_NODE_TYPES:
                continue
            if not query.include_archived and node.status in INACTIVE_STATUSES:
                continue
            if not self._node_matches_query_context_scope(node, scopes):
                continue
            raw_overlap = _raw_query_token_overlap(self._node_search_parts(node), query_profile.informative_tokens)
            if (
                raw_overlap is not None
                and len(query_profile.informative_tokens) >= 4
                and len(raw_overlap) <= 1
                and not self._has_strong_identifier_overlap(raw_overlap)
            ):
                continue
            metrics = self._node_match_metrics(node, query_profile)
            score = metrics["match_score"]
            if score <= 0.0:
                continue
            if self._is_weak_multiterm_match(
                node,
                query_tokens=query_profile.informative_tokens,
                direct_relevance=score,
                overlap_count=int(metrics.get("overlap_count", 0.0)),
                has_strong_identifier_overlap=bool(metrics.get("strong_identifier_overlap", 0.0)),
            ):
                continue
            matches.append((node, score))
        matches.sort(
            key=lambda item: (
                item[1],
                self._retrieval_type_bonus(item[0], item[1]),
                item[0].salience,
                self._node_relative_path(item[0]) or "",
                self._node_label(item[0]),
            ),
            reverse=True,
        )
        return matches[:top_k]

    def _pick_seed_node_ids(self, scored: list[tuple[str, float]], *, max_k: int, gap_ratio: float = 0.20) -> list[str]:
        if not scored:
            return []
        top_score = scored[0][1]
        seeds: list[str] = []
        for node_id, score in scored[:max_k]:
            if seeds and score < top_score * gap_ratio:
                break
            seeds.append(node_id)
        return seeds

    def _expand_and_rank_candidates(
        self,
        seed_node_ids: list[str],
        seed_scores: OrderedDict[str, float],
        query: MemoryQuery,
        query_profile: _QueryProfile,
        *,
        edge_types: set[str],
        code_context: bool,
    ) -> tuple[OrderedDict[str, _PathCandidate], OrderedDict[str, MemoryEdge]]:
        candidates: dict[str, _PathCandidate] = {}
        candidate_edges: OrderedDict[str, MemoryEdge] = OrderedDict()
        queue: list[tuple[str, int, float, float, set[str], list[str]]] = []
        seen_depth: dict[str, int] = {}
        metrics_by_node: dict[str, dict[str, float]] = {}
        overlap_by_node: dict[str, set[str]] = {}

        def metrics_for(node: MemoryNode) -> dict[str, float]:
            metrics = metrics_by_node.get(node.id)
            if metrics is None:
                metrics = self._node_match_metrics(node, query_profile)
                metrics_by_node[node.id] = metrics
            return metrics

        def overlap_for(node: MemoryNode) -> set[str]:
            overlap = overlap_by_node.get(node.id)
            if overlap is None:
                overlap = self._node_query_token_overlap_tokens(node, query_profile.informative_tokens)
                overlap_by_node[node.id] = overlap
            return overlap

        for seed_id in seed_node_ids:
            seed = self.store.get_node(seed_id)
            if seed is None:
                continue
            if not self._candidate_node_allowed(seed, query, code_context=code_context):
                continue
            seed_score = seed_scores.get(seed_id, 0.0)
            seed_tokens = overlap_for(seed)
            self._add_path_candidate(
                candidates,
                seed,
                query_profile,
                seed_score=seed_score,
                path_tokens=seed_tokens,
                depth=0,
                edge_signal=1.0,
                edge_ids=[],
                metrics=metrics_for(seed),
            )
            queue.append((seed_id, 0, seed_score, seed_score, seed_tokens, []))
            seen_depth[seed_id] = 0

        cursor = 0
        while cursor < len(queue):
            current_id, depth, root_seed_score, current_path_score, path_tokens, path_edge_ids = queue[cursor]
            cursor += 1
            if depth >= query.max_depth:
                continue
            neighbors = self.store.neighbors(
                current_id,
                direction="both",
                edge_types=edge_types,
                min_weight=0.01,
                limit=120,
            )
            for edge, neighbor in neighbors:
                if edge.type in TECHNICAL_EDGE_TYPES:
                    continue
                if not self._candidate_node_allowed(neighbor, query, code_context=code_context):
                    continue
                next_depth = depth + 1
                neighbor_tokens = overlap_for(neighbor)
                combined_tokens = set(path_tokens) | neighbor_tokens
                previous_depth = seen_depth.get(neighbor.id)
                existing = candidates.get(neighbor.id)
                if (
                    previous_depth is not None
                    and previous_depth <= next_depth
                    and existing is not None
                    and existing.coverage >= self._coverage(combined_tokens, query_profile)
                ):
                    continue
                metrics = metrics_for(neighbor)
                if (
                    next_depth > 1
                    and metrics["match_score"] <= 0.0
                    and self._coverage(combined_tokens, query_profile) <= self._coverage(path_tokens, query_profile)
                ):
                    continue
                edge_signal = clamp(edge.weight * edge.confidence * max(edge.polarity, 0))
                next_path_score = clamp(0.55 * current_path_score + 0.25 * self._coverage(combined_tokens, query_profile) + 0.20 * edge_signal)
                next_edge_ids = [*path_edge_ids, edge.id]
                candidate_edges.setdefault(edge.id, edge)
                self._add_path_candidate(
                    candidates,
                    neighbor,
                    query_profile,
                    seed_score=root_seed_score,
                    path_tokens=combined_tokens,
                    depth=next_depth,
                    edge_signal=next_path_score,
                    edge_ids=next_edge_ids,
                    metrics=metrics,
                )
                if previous_depth is None or next_depth < previous_depth:
                    seen_depth[neighbor.id] = next_depth
                    queue.append((neighbor.id, next_depth, root_seed_score, next_path_score, combined_tokens, next_edge_ids))

        ordered = OrderedDict(
            sorted(
                candidates.items(),
                key=lambda item: (
                    -item[1].score,
                    item[1].depth_penalty,
                    self._node_relative_path(item[1].node) or "",
                    self._node_label(item[1].node),
                ),
            )
        )
        return ordered, candidate_edges

    def _candidate_node_allowed(self, node: MemoryNode, query: MemoryQuery, *, code_context: bool) -> bool:
        if not self._is_graph_layer_node(node):
            return False
        if code_context and not self._is_code_context_node(node):
            return False
        if query.node_types and node.type not in query.node_types:
            return False
        scopes = self._normalize_query_context_scopes(query.context_scopes)
        if scopes and not self._node_matches_query_context_scope(node, scopes):
            return False
        if not query.include_archived and node.status in INACTIVE_STATUSES:
            return False
        return True

    def _add_path_candidate(
        self,
        candidates: dict[str, _PathCandidate],
        node: MemoryNode,
        query_profile: _QueryProfile,
        *,
        seed_score: float,
        path_tokens: set[str],
        depth: int,
        edge_signal: float,
        edge_ids: list[str],
        metrics: dict[str, float] | None = None,
    ) -> None:
        metrics = metrics or self._node_match_metrics(node, query_profile)
        direct_coverage = metrics["coverage"]
        path_coverage = max(direct_coverage, self._coverage(path_tokens, query_profile))
        type_bonus = self._retrieval_type_bonus(node, metrics["match_score"])
        depth_penalty = min(0.30, depth * 0.08)
        path_score = clamp(0.60 * edge_signal + 0.40 * path_coverage)
        score = clamp(
            0.52 * metrics["match_score"]
            + 0.28 * path_coverage
            + 0.14 * path_score
            + 0.06 * seed_score
            + type_bonus
            - depth_penalty
        )
        if node.type in SOURCE_NODE_TYPES and edge_ids:
            score = clamp(score - 0.08)
        elif node.type in SOURCE_NODE_TYPES:
            score = clamp(score - 0.16)
        if len(query_profile.informative_tokens) >= 4 and metrics["match_score"] < 0.10 and path_coverage < 0.35:
            return
        existing = candidates.get(node.id)
        candidate = _PathCandidate(
            node=node,
            score=score,
            match_score=metrics["match_score"],
            coverage=direct_coverage,
            path_score=path_score,
            type_bonus=type_bonus,
            seed_score=seed_score,
            depth_penalty=depth_penalty,
            edge_ids=edge_ids,
        )
        if existing is None or candidate.score > existing.score:
            candidates[node.id] = candidate

    def _node_match_metrics(self, node: MemoryNode, query_profile: _QueryProfile) -> dict[str, float]:
        query_key = query_profile.canonical
        if not query_key:
            return {"match_score": 0.0, "coverage": 0.0}
        canonical_parts = [
            canonicalize(part)
            for part in (node.canonical_key, node.label, node.text)
            if part
        ]
        if query_key in canonical_parts:
            return {"match_score": 0.98 if node.type == "Topic" else 0.94, "coverage": 1.0}
        node_text = self._node_search_text(node)
        node_key = canonicalize(node_text)
        if not node_key:
            return {"match_score": 0.0, "coverage": 0.0}
        overlap = _canonical_token_overlap(node_key, query_profile.informative_tokens)
        overlap_count = float(len(overlap))
        strong_identifier_overlap = float(self._has_strong_identifier_overlap(overlap))

        def result(match_score: float, coverage: float) -> dict[str, float]:
            return {
                "match_score": match_score,
                "coverage": coverage,
                "overlap_count": overlap_count,
                "strong_identifier_overlap": strong_identifier_overlap,
            }

        coverage = self._coverage(overlap, query_profile)
        phrase_coverage = self._phrase_coverage(node_key, query_profile)
        coverage = max(coverage, phrase_coverage)
        if f" {query_key} " in f" {node_key} ":
            return result(0.86, max(coverage, 0.90))
        if node_key.startswith(query_key) or any(part.startswith(query_key) for part in canonical_parts):
            return result(0.78, max(coverage, 0.80))
        if query_profile.informative_tokens and len(overlap) == len(query_profile.informative_tokens):
            score = 0.76 if phrase_coverage >= 0.50 else 0.70
            return result(score, 1.0)
        if phrase_coverage >= 0.75 and coverage >= 0.50:
            return result(0.68, coverage)
        if phrase_coverage >= 0.50 and coverage >= 0.40:
            return result(0.58, coverage)
        if strong_identifier_overlap:
            return result(0.64, max(coverage, 0.55))
        if coverage >= 0.75:
            return result(0.56, coverage)
        if coverage >= 0.50:
            return result(0.40, coverage)
        if coverage > 0:
            phrase_bonus = 0.18 * phrase_coverage
            return result(min(0.38, (0.16 * coverage) + phrase_bonus), coverage)
        source = str(node.properties.get("relative_path") or node.properties.get("path") or "")
        source_key = canonicalize(" ".join((source, _identifier_expanded_text(source))))
        source_overlap = _canonical_token_overlap(source_key, query_profile.informative_tokens)
        source_coverage = self._coverage(source_overlap, query_profile)
        if source_coverage >= 0.50:
            return result(0.30 * source_coverage, source_coverage)
        return result(0.0, 0.0)

    @staticmethod
    def _coverage(tokens: set[str], query_profile: _QueryProfile) -> float:
        if not query_profile.informative_tokens:
            return 0.0
        return min(1.0, len(tokens & query_profile.informative_tokens) / len(query_profile.informative_tokens))

    @staticmethod
    def _phrase_coverage(node_key: str, query_profile: _QueryProfile) -> float:
        if not query_profile.phrase_terms:
            return 0.0
        haystack = f" {node_key} "
        matched = sum(1 for phrase in query_profile.phrase_terms if f" {phrase} " in haystack)
        return min(1.0, matched / len(query_profile.phrase_terms))

    def _significant_query_phrases(self, query_text: str) -> list[str]:
        tokens = [
            token
            for token in _expanded_tokens(query_text)
            if len(token) >= 4 or token_signal_score(token) >= 0.75
        ]
        phrases: list[str] = []
        seen: set[str] = set()
        for size in (4, 3, 2):
            for index in range(0, max(0, len(tokens) - size + 1)):
                phrase = " ".join(tokens[index : index + size])
                key = canonicalize(phrase)
                if key and key not in seen:
                    seen.add(key)
                    phrases.append(key)
        return phrases

    @staticmethod
    def _retrieval_type_bonus(node: MemoryNode, match_score: float) -> float:
        if match_score <= 0.0:
            return 0.0
        if node.type in {"Function", "Method", "Class", "Interface", "Module"}:
            return 0.10
        if node.type in {"SourceArtifact", "File", "Endpoint", "Schema", "Config", "Test"}:
            return 0.07
        if node.type in {"Variable", "Import", "Dependency", "StaticAnalysisFinding"}:
            return 0.04
        if node.type in {"SourceFragment", "DocumentFragment"}:
            return 0.02
        return 0.0

    def _direct_relevance_score(self, node: MemoryNode, query_text: str, *, query_tokens: set[str] | None = None) -> float:
        profile = self._query_profile(query_text)
        if query_tokens is not None:
            profile.tokens = query_tokens
            profile.informative_tokens = {
                token
                for token in query_tokens
                if len(token) >= 3 or token_signal_score(token) >= 0.85
            } or query_tokens
            profile.ordered_tokens = tuple(token for token in profile.ordered_tokens if token in query_tokens)
            profile.phrase_terms = {
                f"{a} {b}"
                for a, b in zip(profile.ordered_tokens, profile.ordered_tokens[1:])
                if token_signal_score(a) >= 0.5 and token_signal_score(b) >= 0.5
            }
        return self._node_match_metrics(node, profile)["match_score"]

    def _is_weak_multiterm_match(
        self,
        node: MemoryNode,
        *,
        query_tokens: set[str],
        direct_relevance: float,
        overlap_count: int | None = None,
        has_strong_identifier_overlap: bool | None = None,
    ) -> bool:
        if len(query_tokens) < 4 or direct_relevance >= 0.10:
            return False
        overlap_tokens: set[str] | None = None
        if overlap_count is None or has_strong_identifier_overlap is None:
            overlap_tokens = self._node_query_token_overlap_tokens(node, query_tokens)
        if has_strong_identifier_overlap is None:
            has_strong_identifier_overlap = self._has_strong_identifier_overlap(overlap_tokens or set())
        if has_strong_identifier_overlap:
            return False
        overlap = overlap_count if overlap_count is not None else len(overlap_tokens or set())
        if overlap <= 1:
            return True
        return (overlap / len(query_tokens)) < 0.25

    @staticmethod
    def _node_search_parts(node: MemoryNode) -> list[str]:
        parts: list[str] = []
        for part in (node.text, node.label, node.canonical_key):
            if part:
                parts.append(str(part))
        for key in STRUCTURED_SEARCH_FIELDS:
            value = node.properties.get(key)
            if value is None:
                continue
            if isinstance(value, (list, tuple, set)):
                parts.extend(str(item) for item in value if item)
            else:
                parts.append(str(value))
        return parts

    @classmethod
    def _node_search_text(cls, node: MemoryNode) -> str:
        parts = cls._node_search_parts(node)
        expanded_parts = [_identifier_expanded_text(part) for part in parts]
        return " ".join([*parts, *expanded_parts])

    @classmethod
    def _node_query_token_overlap(cls, node: MemoryNode, query_tokens: set[str]) -> int:
        return len(cls._node_query_token_overlap_tokens(node, query_tokens))

    @classmethod
    def _node_query_token_overlap_tokens(cls, node: MemoryNode, query_tokens: set[str]) -> set[str]:
        if not query_tokens:
            return set()
        node_text = cls._node_search_text(node)
        node_key = canonicalize(node_text)
        return _canonical_token_overlap(node_key, query_tokens)

    @staticmethod
    def _has_strong_identifier_overlap(tokens: set[str]) -> bool:
        return any(
            token_signal_score(token) >= 0.85 and (any(separator in token for separator in ("_", "-")) or any(char.isdigit() for char in token))
            for token in tokens
        )

    @staticmethod
    def _contains_query(
        node_key: str,
        query_key: str,
        *,
        node_tokens: set[str] | None = None,
        query_tokens: set[str] | None = None,
    ) -> bool:
        if not query_key:
            return False
        if node_key == query_key:
            return True
        if f" {query_key} " in f" {node_key} ":
            return True
        node_tokens = node_tokens if node_tokens is not None else set(tokenize(node_key))
        query_tokens = query_tokens if query_tokens is not None else set(tokenize(query_key))
        return bool(query_tokens) and query_tokens.issubset(node_tokens)

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

    def _code_agent_context_payload(
        self,
        subgraph: MemorySubgraph,
        *,
        query_mode: str,
        max_items: int,
        include_risky: bool = False,
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
            include_risky=include_risky,
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
                include_risky=include_risky,
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
            matches.append((metrics["match_score"], node))
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
        if metrics["match_score"] >= 0.50:
            return True
        fragment_key = canonicalize(text)
        if not fragment_key:
            return False
        query_phrases = self._significant_query_phrases(query_text)
        return any(phrase in fragment_key for phrase in query_phrases)

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
        include_risky: bool = False,
    ) -> list[dict[str, Any]]:
        ranked_by_id = {item.node.id: item for item in ranked}
        query_tokens = set(_expanded_tokens(subgraph.query.text))
        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()
        for node in [*(item.node for item in ranked), *subgraph.nodes]:
            if node.id in seen or node.type != "StaticAnalysisFinding":
                continue
            seen.add(node.id)
            if not include_risky and not self._is_safe_cleanup_candidate(node) and not self._is_aggregate_cleanup_candidate(node):
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

    def _filter_cleanup_candidate_payloads(self, candidates: list[dict[str, Any]], *, include_risky: bool) -> list[dict[str, Any]]:
        if include_risky:
            return candidates
        safe: list[dict[str, Any]] = []
        for item in candidates:
            node = self.store.get_node(str(item.get("id") or ""))
            if node is not None and (self._is_safe_cleanup_candidate(node) or self._is_aggregate_cleanup_candidate(node)):
                safe.append(item)
        return safe

    @staticmethod
    def _cleanup_filter_payload(*, include_risky: bool, total_candidates: int, shown_candidates: int) -> dict[str, Any]:
        return {
            "mode": "include_risky" if include_risky else "safe_remove",
            "include_risky": include_risky,
            "shown_candidates": shown_candidates,
            "excluded_risky_candidates": max(0, total_candidates - shown_candidates) if not include_risky else 0,
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
            test_subgraph = self.retrieve(
                MemoryQuery(
                    text=query_text,
                    top_k=max(8, min(max_items * 2, 20)),
                    max_depth=1,
                    include_archived=subgraph.query.include_archived,
                    context_scopes={"test"},
                    store_trace=False,
                )
            )
            for item in test_subgraph.ranked_nodes:
                ranked_by_id[item.node.id] = item
                candidate_nodes.setdefault(item.node.id, item.node)
            for node in test_subgraph.nodes:
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
            include_note = "validate/risky findings excluded" if not cleanup_filter.get("include_risky") else "validate/risky findings included"
            self._append_section(lines, "Cleanup filter", [f"- mode={mode}; shown={cleanup_filter.get('shown_candidates', 0)}; excluded_risky={excluded}; {include_note}"])
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
