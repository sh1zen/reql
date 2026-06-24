"""Deterministic local document processing.

This layer turns parsed document fragments into ranked term observations and
term-to-term relations without language-specific rules or model calls.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
import math
import unicodedata
from typing import TYPE_CHECKING, Any

from .normalization import clamp, normalize_text, short_label, token_signal_score

if TYPE_CHECKING:
    from ..document_ingestion.models import DocumentFragment


@dataclass(frozen=True, slots=True)
class DocumentTerm:
    key: str
    label: str
    rank: float
    term_frequency: float
    fragment_count: int
    first_fragment_id: str
    evidence: str
    term_type: str
    raw_events: list["DocumentRawEvent"] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class DocumentRawEvent:
    id_key: str
    event_type: str
    fragment_id: str
    term_key: str
    term_label: str
    occurrence_count: float
    rank: float
    evidence: str
    start_line: int | None
    end_line: int | None


@dataclass(frozen=True, slots=True)
class DocumentTermRelation:
    source_key: str
    target_key: str
    relation: str
    rank: float
    cooccurrence_count: float
    fragment_count: int
    evidence_fragment_id: str
    evidence: str


@dataclass(frozen=True, slots=True)
class DocumentProcessingResult:
    terms: list[DocumentTerm]
    relations: list[DocumentTermRelation]
    raw_events: list[DocumentRawEvent]
    signature: dict[str, Any]


class DocumentProcessor:
    """Build a language-agnostic deterministic concept layer from fragments."""

    def __init__(self, *, max_terms: int = 32, max_relations: int = 96, max_events_per_term: int = 2) -> None:
        self.max_terms = max_terms
        self.max_relations = max_relations
        self.max_events_per_term = max_events_per_term

    def process(self, fragments: list[DocumentFragment]) -> DocumentProcessingResult:
        indexed = [_fragment_terms(fragment) for fragment in fragments if str(fragment.text or "").strip()]
        indexed = [item for item in indexed if item["tokens"]]
        if not indexed:
            return DocumentProcessingResult(terms=[], relations=[], raw_events=[], signature={"term_count": 0, "relation_count": 0})

        term_counts: Counter[str] = Counter()
        term_labels: dict[str, str] = {}
        term_fragments: dict[str, set[str]] = defaultdict(set)
        term_first_fragment: dict[str, str] = {}
        term_evidence: dict[str, str] = {}
        term_fragment_counts: dict[tuple[str, str], float] = defaultdict(float)

        for item in indexed:
            fragment = item["fragment"]
            terms = item["terms"]
            for term, count in terms.items():
                key = _document_key(term)
                if not _useful_document_term(key):
                    continue
                term_counts[key] += count
                term_labels.setdefault(key, term)
                term_fragments[key].add(fragment.id)
                term_first_fragment.setdefault(key, fragment.id)
                term_evidence.setdefault(key, short_label(str(fragment.text or ""), 220))
                term_fragment_counts[(fragment.id, key)] += count

        if not term_counts:
            return DocumentProcessingResult(terms=[], relations=[], raw_events=[], signature={"term_count": 0, "relation_count": 0})

        max_count = max(term_counts.values()) or 1.0
        fragment_total = max(1, len(indexed))
        ranked_terms: list[DocumentTerm] = []
        for key, count in term_counts.items():
            fragment_count = len(term_fragments[key])
            score = _term_rank(key, count, max_count=max_count, fragment_count=fragment_count, fragment_total=fragment_total)
            ranked_terms.append(
                DocumentTerm(
                    key=key,
                    label=term_labels[key],
                    rank=score,
                    term_frequency=float(count),
                    fragment_count=fragment_count,
                    first_fragment_id=term_first_fragment[key],
                    evidence=term_evidence[key],
                    term_type="phrase" if " " in key else "token",
                )
            )
        ranked_terms.sort(key=lambda item: (item.rank, item.term_frequency, len(item.key)), reverse=True)
        ranked_terms = ranked_terms[: self.max_terms]
        ranked_keys = {term.key for term in ranked_terms}

        raw_events: list[DocumentRawEvent] = []
        event_map: dict[str, list[DocumentRawEvent]] = defaultdict(list)
        fragment_by_id = {item["fragment"].id: item["fragment"] for item in indexed}
        for term in ranked_terms:
            fragment_counts = [
                (count, fragment_id)
                for (fragment_id, key), count in term_fragment_counts.items()
                if key == term.key
            ]
            fragment_counts.sort(key=lambda item: (-item[0], item[1]))
            for count, fragment_id in fragment_counts[: self.max_events_per_term]:
                fragment = fragment_by_id.get(fragment_id)
                if fragment is None:
                    continue
                event = DocumentRawEvent(
                    id_key=f"{fragment_id}:{term.key}",
                    event_type="document_term_observation",
                    fragment_id=fragment_id,
                    term_key=term.key,
                    term_label=term.label,
                    occurrence_count=float(count),
                    rank=clamp(term.rank * min(1.0, float(count) / max_count + 0.25)),
                    evidence=short_label(str(fragment.text or ""), 260),
                    start_line=fragment.start_line,
                    end_line=fragment.end_line,
                )
                raw_events.append(event)
                event_map[term.key].append(event)

        ranked_terms = [
            DocumentTerm(
                key=term.key,
                label=term.label,
                rank=term.rank,
                term_frequency=term.term_frequency,
                fragment_count=term.fragment_count,
                first_fragment_id=term.first_fragment_id,
                evidence=term.evidence,
                term_type=term.term_type,
                raw_events=list(event_map.get(term.key, [])),
            )
            for term in ranked_terms
        ]

        relations = self._relations(indexed, ranked_keys, ranked_terms)
        signature = {
            "term_count": len(ranked_terms),
            "relation_count": len(relations),
            "raw_event_count": len(raw_events),
            "top_terms": [term.key for term in ranked_terms[:10]],
        }
        return DocumentProcessingResult(terms=ranked_terms, relations=relations, raw_events=raw_events, signature=signature)

    def _relations(
        self,
        indexed: list[dict[str, Any]],
        ranked_keys: set[str],
        ranked_terms: list[DocumentTerm],
    ) -> list[DocumentTermRelation]:
        term_rank = {term.key: term.rank for term in ranked_terms}
        pair_counts: Counter[tuple[str, str]] = Counter()
        pair_fragments: dict[tuple[str, str], set[str]] = defaultdict(set)
        pair_evidence: dict[tuple[str, str], str] = {}
        for item in indexed:
            fragment = item["fragment"]
            present = sorted(key for key in item["terms"] if key in ranked_keys)
            for index, source in enumerate(present):
                for target in present[index + 1 :]:
                    if source == target:
                        continue
                    pair = (source, target)
                    pair_counts[pair] += 1
                    pair_fragments[pair].add(fragment.id)
                    pair_evidence.setdefault(pair, short_label(str(fragment.text or ""), 260))
        if not pair_counts:
            return []
        max_count = max(pair_counts.values()) or 1
        relations: list[DocumentTermRelation] = []
        for pair, count in pair_counts.items():
            source, target = pair
            rank = clamp((count / max_count) * 0.55 + ((term_rank.get(source, 0.0) + term_rank.get(target, 0.0)) / 2.0) * 0.45)
            if rank < 0.18:
                continue
            if term_rank.get(target, 0.0) > term_rank.get(source, 0.0):
                source, target = target, source
            fragment_ids = sorted(pair_fragments.get(pair, set()))
            relations.append(
                DocumentTermRelation(
                    source_key=source,
                    target_key=target,
                    relation="co_occurs",
                    rank=rank,
                    cooccurrence_count=float(count),
                    fragment_count=len(fragment_ids),
                    evidence_fragment_id=fragment_ids[0] if fragment_ids else "",
                    evidence=pair_evidence.get(pair, ""),
                )
            )
        relations.sort(key=lambda item: (item.rank, item.cooccurrence_count), reverse=True)
        return relations[: self.max_relations]


def _fragment_terms(fragment: DocumentFragment) -> dict[str, Any]:
    text = normalize_text(str(fragment.text or ""))
    tokens = _document_tokens(text)
    counts: Counter[str] = Counter(token for token in tokens if _useful_document_term(token))
    for left, right in zip(tokens, tokens[1:]):
        if token_signal_score(left) >= 0.5 and token_signal_score(right) >= 0.5:
            counts[f"{left} {right}"] += 1.4
    for first, second, third in zip(tokens, tokens[1:], tokens[2:]):
        if min(token_signal_score(first), token_signal_score(second), token_signal_score(third)) >= 0.65:
            counts[f"{first} {second} {third}"] += 1.8
    return {"fragment": fragment, "tokens": tokens, "terms": counts}


def _useful_document_term(term: str) -> bool:
    compact = _document_key(term)
    if compact.isdigit():
        return False
    if _has_non_latin_alnum(compact) and len(compact.replace(" ", "")) >= 2:
        return True
    if len(compact) < 3:
        return False
    if len(set(compact.replace(" ", ""))) < 3:
        return False
    parts = compact.split()
    if len(parts) == 1:
        return token_signal_score(compact) >= 0.45
    return all(token_signal_score(part) >= 0.35 for part in parts)


def _document_tokens(value: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    raw_tokens: list[str] = []
    current: list[str] = []
    for char in normalized:
        if char.isalnum() or char in {"_", "-", ".", "/", "\\", "#", "+"}:
            current.append(char)
            continue
        if current:
            raw_tokens.append("".join(current).strip("_-.+/\\#"))
            current = []
    if current:
        raw_tokens.append("".join(current).strip("_-.+/\\#"))
    return [token for token in raw_tokens if len(token) >= 2 and token_signal_score(token) > 0.0]


def _document_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", normalize_text(value)).casefold()
    chars = [char if (char.isalnum() or char in {"_", "-"}) else " " for char in normalized]
    return " ".join("".join(chars).split())


def _has_non_latin_alnum(value: str) -> bool:
    return any(char.isalnum() and ord(char) > 127 for char in value)


def _term_rank(term: str, count: float, *, max_count: float, fragment_count: int, fragment_total: int) -> float:
    specificity = sum(token_signal_score(part) for part in term.split()) / max(1, len(term.split()))
    phrase_boost = 0.14 if " " in term else 0.0
    distribution = math.log1p(fragment_count) / math.log1p(fragment_total)
    frequency = math.log1p(count) / math.log1p(max_count)
    saturation_penalty = 0.85 if fragment_count == fragment_total and fragment_total > 2 else 1.0
    return clamp((frequency * 0.48 + specificity * 0.34 + distribution * 0.18 + phrase_boost) * saturation_penalty)
