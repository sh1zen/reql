"""Deterministic lexical search and ranking primitives."""
from __future__ import annotations

from typing import MutableMapping, Sequence

from ...domain.constants import INACTIVE_STATUSES
from ...domain.models import MemoryNode, MemoryQuery
from ...extraction.normalization import canonicalize, token_signal_score, tokenize
from .common import (
    STRUCTURED_SEARCH_FIELDS,
    TECHNICAL_NODE_TYPES,
    _canonical_token_overlap,
    _expanded_tokens,
    _identifier_expanded_text,
    _raw_query_token_overlap,
)
from .context.models import NodeMatchMetrics, QueryProfile


class RetrievalSearchMixin:
    def _query_profile(self, query_text: str) -> QueryProfile:
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
        return QueryProfile(
            text=query_text,
            canonical=canonicalize(query_text),
            tokens=tokens,
            informative_tokens=informative or tokens,
            ordered_tokens=ordered_tokens,
            phrase_terms=phrase_terms,
        )

    def _nodes_for_types(self, node_types: Sequence[str]) -> list[MemoryNode]:
        """Load only indexed node types while preserving ``all_nodes`` order."""
        find_many = getattr(self.store, "find_nodes_by_types", None)
        if callable(find_many):
            return find_many(node_types, clone=False)
        nodes: dict[str, MemoryNode] = {}
        for node_type in sorted(set(node_types)):
            for node in self.store.find_nodes(type_=node_type, limit=2**63 - 1, clone=False):
                nodes.setdefault(node.id, node)
        return sorted(nodes.values(), key=lambda node: (node.created_at, node.id))

    def _scoped_lexical_search(
        self,
        query: MemoryQuery,
        query_profile: QueryProfile,
        *,
        lexical_node_types: Sequence[str] | None,
        scopes: set[str],
        top_k: int,
        metrics_cache: MutableMapping[str, NodeMatchMetrics] | None = None,
    ) -> list[tuple[MemoryNode, float]]:
        allowed_types = set(lexical_node_types) if lexical_node_types is not None else None
        candidate_limit = max(top_k * (8 if scopes & {"docs", "test"} else 4), 160)
        try:
            indexed = self.store.lexical_search(
                query.text,
                top_k=candidate_limit,
                node_types=allowed_types,
                include_archived=query.include_archived,
                clone=False,
            )
        except TypeError:
            # Preserve compatibility with external GraphStore adapters that
            # implement the pre-clone keyword signature.
            indexed = self.store.lexical_search(
                query.text,
                top_k=candidate_limit,
                node_types=allowed_types,
                include_archived=query.include_archived,
            )
        candidates = [node for node, _score in indexed]
        if not candidates:
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
            metrics = metrics_cache.get(node.id) if metrics_cache is not None else None
            if metrics is None:
                metrics = self._node_match_metrics(node, query_profile)
                if metrics_cache is not None:
                    metrics_cache[node.id] = metrics
            score = metrics.match_score
            if score <= 0.0:
                continue
            if self._is_weak_multiterm_match(
                node,
                query_tokens=query_profile.informative_tokens,
                direct_relevance=score,
                overlap_count=metrics.overlap_count,
                has_strong_identifier_overlap=metrics.strong_identifier_overlap,
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


    def _node_match_metrics(self, node: MemoryNode, query_profile: QueryProfile) -> NodeMatchMetrics:
        query_key = query_profile.canonical
        if not query_key:
            return NodeMatchMetrics(0.0, 0.0)
        canonical_parts = [
            canonicalize(part)
            for part in (node.canonical_key, node.label, node.text)
            if part
        ]
        if query_key in canonical_parts:
            return NodeMatchMetrics(0.98 if node.type == "Topic" else 0.94, 1.0, frozenset(query_profile.informative_tokens))
        node_text = self._node_search_text(node)
        node_key = canonicalize(node_text)
        if not node_key:
            return NodeMatchMetrics(0.0, 0.0)
        overlap = _canonical_token_overlap(node_key, query_profile.informative_tokens)
        strong_identifier_overlap = self._has_strong_identifier_overlap(overlap)
        identifier_keys = [
            canonicalize(_identifier_expanded_text(part))
            for part in self._node_identifier_parts(node)
            if part
        ]
        identifier_overlap = _canonical_token_overlap(" ".join(identifier_keys), query_profile.informative_tokens)
        identifier_coverage = self._coverage(identifier_overlap, query_profile)

        def result(match_score: float, coverage: float) -> NodeMatchMetrics:
            return NodeMatchMetrics(match_score, coverage, frozenset(overlap), strong_identifier_overlap)

        coverage = self._coverage(overlap, query_profile)
        phrase_coverage = self._phrase_coverage(node_key, query_profile)
        coverage = max(coverage, phrase_coverage)
        if query_key in identifier_keys:
            return result(0.92, 1.0)
        if any(f" {query_key} " in f" {key} " for key in identifier_keys):
            return result(0.86, max(coverage, identifier_coverage, 0.90))
        if identifier_coverage >= 1.0:
            return result(0.82 if phrase_coverage >= 0.50 else 0.78, 1.0)
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
    def _coverage(tokens: set[str], query_profile: QueryProfile) -> float:
        if not query_profile.informative_tokens:
            return 0.0
        return min(1.0, len(tokens & query_profile.informative_tokens) / len(query_profile.informative_tokens))

    @staticmethod
    def _phrase_coverage(node_key: str, query_profile: QueryProfile) -> float:
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
        return self._node_match_metrics(node, profile).match_score

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
    def _node_identifier_parts(node: MemoryNode) -> list[str]:
        parts: list[str] = []
        for part in (node.label, node.canonical_key):
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
    def _node_search_parts(cls, node: MemoryNode) -> list[str]:
        parts = [str(node.text)] if node.text else []
        parts.extend(cls._node_identifier_parts(node))
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

