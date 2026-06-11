"""Semantic specificity and generic-node penalties."""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

from ..domain.models import MemoryNode
from ..ports.graph_store import GraphStore

GENERIC_TYPES = {"GraphDelta", "ArtifactCacheEntry", "SourceFragment", "URI"}
SPECIFIC_TYPES = {"Function", "Class", "Method", "Module", "SourceArtifact", "Concept", "File", "StaticAnalysisFinding"}


@dataclass(frozen=True, slots=True)
class SpecificityScore:
    node_id: str
    specificity: float
    generic_penalty: float
    reasons: list[str]


class SpecificityScorer:
    def __init__(self, store: GraphStore) -> None:
        self.store = store

    def score(self, node: MemoryNode) -> SpecificityScore:
        text = " ".join(str(value or "") for value in [node.label, node.text, node.canonical_key, node.properties.get("name"), node.properties.get("qualified_name")]).strip()
        tokens = _tokens(text)
        reasons: list[str] = []
        specificity = 0.35
        penalty = 0.0

        if 2 <= len(tokens) <= 8:
            specificity += 0.20
            reasons.append("concise specific label")
        elif len(tokens) > 14:
            penalty += 0.10
            reasons.append("overlong label")
        elif len(tokens) <= 1:
            specificity += 0.05 if tokens and len(tokens[0]) >= 5 else 0.0

        if tokens:
            weak_ratio = sum(1 for token in tokens if _weak_token(token)) / len(tokens)
            penalty += 0.35 * weak_ratio
            if weak_ratio > 0.45:
                reasons.append("weak token pattern")
            if any(_specific_token(token) for token in tokens):
                specificity += 0.15
                reasons.append("topical terms")

        if node.type in SPECIFIC_TYPES:
            specificity += 0.15
            reasons.append(f"specific node type {node.type}")
        if node.type in GENERIC_TYPES:
            penalty += 0.25
            reasons.append(f"generic node type {node.type}")
        if node.type == "CodeSymbol" and (node.properties.get("external") or node.properties.get("synthetic") or node.properties.get("kind") in {"external", "decorator"}):
            penalty += 0.45
            reasons.append("external code symbol")

        neighbor_types = _neighbor_type_counts(self.store, node)
        entropy = _entropy(list(neighbor_types.values()))
        if entropy > 1.5:
            penalty += 0.12
            reasons.append("high neighbor type entropy")
        elif neighbor_types:
            specificity += 0.08
            reasons.append("focused neighborhood")

        degree = sum(neighbor_types.values())
        if degree > 8 and specificity < 0.55:
            penalty += min(0.25, degree / 80)
            reasons.append("high degree with low topical focus")

        artifact_type = str(node.properties.get("artifact_type") or "")
        if artifact_type in {"binary", "unknown"}:
            penalty += 0.08
        elif artifact_type in {"code", "markdown", "text"}:
            specificity += 0.04

        usage_success = float(node.properties.get("usage_success") or 0)
        if usage_success > 0:
            specificity += min(0.10, usage_success / 10)

        return SpecificityScore(node.id, max(0.0, min(1.0, specificity)), max(0.0, min(1.0, penalty)), reasons or ["neutral specificity"])


def _tokens(text: str) -> list[str]:
    return [token.casefold() for token in re.findall(r"[A-Za-z0-9_]+", text)]


def _weak_token(token: str) -> bool:
    return len(token) <= 3 and not any(char.isdigit() for char in token) and "_" not in token


def _specific_token(token: str) -> bool:
    return len(token) >= 6 or any(char.isdigit() for char in token) or "_" in token


def _neighbor_type_counts(store: GraphStore, node: MemoryNode) -> Counter[str]:
    counts: Counter[str] = Counter()
    for edge in store.get_edges(from_id=node.id, limit=200):
        neighbor = store.get_node(edge.to_id)
        if neighbor:
            counts[neighbor.type] += 1
    for edge in store.get_edges(to_id=node.id, limit=200):
        neighbor = store.get_node(edge.from_id)
        if neighbor:
            counts[neighbor.type] += 1
    return counts


def _entropy(values: list[int]) -> float:
    total = sum(values)
    if total <= 0:
        return 0.0
    entropy = 0.0
    for value in values:
        p = value / total
        entropy -= p * math.log2(p)
    return entropy
