"""Deterministic query/source extraction for the code graph.

The extractor is intentionally small and dependency-free. It provides lexical
topics and named-entity seeds for retrieval without producing conversation
memory candidates such as claims, preferences, observations, or corrections.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .normalization import canonicalize, extract_capitalized_entities, keyword_scores, normalize_text, split_sentences


@dataclass(slots=True)
class ExtractedEntityMention:
    text: str
    canonical_key: str
    entity_type: str
    confidence: float
    resolved_key: str
    source_sentence: str
    extractor: str
    start_char: int | None = None
    end_char: int | None = None
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ResolvedEntity:
    canonical_key: str
    canonical_name: str
    entity_type: str
    confidence: float
    aliases: list[str]
    mention_count: int
    provenance: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class ExtractedRelation:
    subject: str
    predicate: str
    object_value: str
    subject_key: str
    object_key: str
    edge_type: str
    confidence: float
    source_sentence: str
    raw_predicate: str
    normalized_predicate: str
    language: str
    extractor: str = "deterministic"
    dynamic_relation: bool = False
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ExtractionResult:
    text: str
    semantic_hash: str
    topics: list[tuple[str, float]]
    entities: list[tuple[str, str, float]]
    candidates: list[Any] = field(default_factory=list)
    sentences: list[str] = field(default_factory=list)
    quality: float = 0.0
    relations: list[ExtractedRelation] = field(default_factory=list)
    entity_mentions: list[ExtractedEntityMention] = field(default_factory=list)
    resolved_entities: list[ResolvedEntity] = field(default_factory=list)
    language: str | None = None
    signature: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class DeterministicExtractor:
    """Rule-based extractor used for query seed discovery."""

    def extract(self, text: str) -> ExtractionResult:
        text = normalize_text(text)
        semantic_hash = canonicalize(text)
        sentences = split_sentences(text)
        topics = self._extract_topics(text)
        entities = self._extract_entities(text)
        signature = {
            "normalized_tokens": [topic for topic, _ in topics],
            "main_topics": [topic for topic, _ in topics[:8]],
            "entity_canonical_keys": sorted({canonicalize(entity) for entity, _, _ in entities if canonicalize(entity)}),
            "relation_predicates": [],
        }
        quality = min(1.0, 0.25 + 0.15 * len(topics) + 0.15 * len(entities))
        return ExtractionResult(
            text=text,
            semantic_hash=semantic_hash,
            topics=topics,
            entities=entities,
            candidates=[],
            sentences=sentences,
            quality=quality,
            signature=signature,
        )

    def _extract_topics(self, text: str) -> list[tuple[str, float]]:
        topics: list[tuple[str, float]] = []
        for term, score in keyword_scores(text, max_terms=14):
            if len(term) < 3:
                continue
            if " " not in term and score < 0.45:
                continue
            topics.append((term, score))
        return topics[:10]

    def _extract_entities(self, text: str) -> list[tuple[str, str, float]]:
        entities: list[tuple[str, str, float]] = []
        for entity in extract_capitalized_entities(text):
            key = canonicalize(entity)
            if not key or key in {"ok"}:
                continue
            kind = "technology" if any(ch.isdigit() for ch in entity) or key in {"neo4j", "sqlite", "llm"} else "named_thing"
            score = 0.85 if kind == "technology" else 0.65
            entities.append((entity, kind, score))
        return entities[:16]
