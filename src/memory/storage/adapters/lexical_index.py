"""Bounded lexical term selection for the block graph store."""
from __future__ import annotations

from collections import Counter
from typing import Sequence

from ...domain.models import MemoryNode
from ...extraction.normalization import (
    identifier_expanded_text,
    keyword_scores,
    token_signal_score,
    tokenize,
    token_variants,
)

LEXICAL_INDEX_SCHEMA_VERSION = 2
LEXICAL_TEXT_WINDOW_COUNT = 8
LEXICAL_TEXT_TOKEN_BUDGET = 384
LEXICAL_TEXT_KEYWORD_BUDGET = 96
LEXICAL_TEXT_TERM_BUDGET = 448
LEXICAL_METADATA_TOKEN_BUDGET = 96
LEXICAL_METADATA_KEYWORD_BUDGET = 48
LEXICAL_METADATA_TERM_BUDGET = 128
LEXICAL_MAX_TERMS_PER_NODE = 576
LEXICAL_PROPERTY_VALUE_LIMIT = 64
LEXICAL_WINDOW_OVERLAP = 48


def _text_term_weights(value: str) -> dict[str, float]:
    """Select weighted terms with coverage across long source text."""
    windows = _text_windows(value)
    if not windows:
        return {}

    token_quota = max(1, LEXICAL_TEXT_TOKEN_BUDGET // len(windows))
    keyword_quota = max(1, LEXICAL_TEXT_KEYWORD_BUDGET // len(windows))
    weights: dict[str, float] = {}
    for window in windows:
        for token, weight in _selected_token_weights(window, token_quota).items():
            weights[token] = max(weights.get(token, 0.0), weight)
        for term, score in keyword_scores(window, max_terms=keyword_quota):
            weights[term] = max(weights.get(term, 0.0), float(score))
    return _cap_terms(weights, LEXICAL_TEXT_TERM_BUDGET)


def _metadata_term_weights(node: MemoryNode, property_values: Sequence[str]) -> dict[str, float]:
    """Weight bounded identifiers and searchable properties above prose."""
    values = [node.type, node.label or "", node.canonical_key or "", *property_values]
    text = " ".join(value for value in values if value)
    if not text:
        return {}
    weights = {
        term: min(1.20, weight * 1.20)
        for term, weight in _selected_token_weights(text, LEXICAL_METADATA_TOKEN_BUDGET).items()
    }
    for term, score in keyword_scores(text, max_terms=LEXICAL_METADATA_KEYWORD_BUDGET):
        weights[term] = max(weights.get(term, 0.0), min(1.20, float(score) * 1.20))
    expanded_text = identifier_expanded_text(text)
    if expanded_text != text:
        for term, score in keyword_scores(expanded_text, max_terms=LEXICAL_METADATA_KEYWORD_BUDGET):
            weights[term] = max(weights.get(term, 0.0), min(1.20, float(score) * 1.20))
    return _cap_terms(weights, LEXICAL_METADATA_TERM_BUDGET)


def _selected_token_weights(value: str, limit: int) -> dict[str, float]:
    """Expand identifiers and choose a bounded set of weighted tokens."""
    tokens = tokenize(value)
    expanded = tokenize(identifier_expanded_text(value))
    existing = set(tokens)
    tokens.extend(token for token in expanded if token not in existing)
    tokens = [variant for token in tokens for variant in token_variants(token)]
    if not tokens or limit <= 0:
        return {}

    counts = Counter(tokens)
    ordered = list(dict.fromkeys(tokens))
    selected = _select_tokens(ordered, counts, limit)
    return {
        token: _token_index_weight(token, counts[token])
        for token in selected
    }


def _select_tokens(ordered: list[str], counts: Counter[str], limit: int) -> list[str]:
    """Balance rare, high-signal tokens with positional coverage."""
    if len(ordered) <= limit:
        return ordered

    priority_slots = max(1, int(limit * 0.75))
    priority = sorted(
        ordered,
        key=lambda token: (
            token_signal_score(token),
            1.0 / max(1, counts[token]),
            len(token),
        ),
        reverse=True,
    )[:priority_slots]
    selected = list(priority)
    selected_set = set(selected)

    remaining = [token for token in ordered if token not in selected_set]
    slots = max(0, limit - len(selected))
    if not remaining or slots <= 0:
        return selected[:limit]
    if len(remaining) <= slots:
        selected.extend(remaining)
        return selected[:limit]

    # Preserve positional coverage as well as high-signal/rare terms.  This is
    # what prevents terms near the middle or tail of a long PDF page from being
    # systematically invisible to lexical retrieval.
    for index in range(slots):
        position = min(len(remaining) - 1, (index * len(remaining)) // slots)
        token = remaining[position]
        if token not in selected_set:
            selected.append(token)
            selected_set.add(token)
    if len(selected) < limit:
        for token in remaining:
            if token in selected_set:
                continue
            selected.append(token)
            if len(selected) >= limit:
                break
    return selected[:limit]


def _token_index_weight(token: str, count: int) -> float:
    """Combine signal and rarity into a bounded lexical weight."""
    signal = token_signal_score(token)
    rarity = 1.0 / max(1.0, float(count) ** 0.5)
    return min(1.0, 0.30 + (0.55 * signal) + (0.15 * rarity))


def _text_windows(value: str) -> list[str]:
    """Split text into bounded overlapping windows without truncating its tail."""
    text = str(value or "")
    if not text:
        return []
    if len(text) <= 2048:
        return [text]

    count = min(LEXICAL_TEXT_WINDOW_COUNT, max(2, (len(text) + 2047) // 2048))
    windows: list[str] = []
    for index in range(count):
        start = (index * len(text)) // count
        end = ((index + 1) * len(text)) // count
        if index:
            start = max(0, start - LEXICAL_WINDOW_OVERLAP)
        if index + 1 < count:
            end = min(len(text), end + LEXICAL_WINDOW_OVERLAP)
        window = text[start:end]
        if window:
            windows.append(window)
    return windows


def _cap_terms(weights: dict[str, float], limit: int) -> dict[str, float]:
    """Keep the strongest terms with deterministic tie breaking."""
    if len(weights) <= limit:
        return weights
    ranked = sorted(
        weights.items(),
        key=lambda item: (
            item[1],
            token_signal_score(item[0].replace(" ", "_")),
            len(item[0]),
            item[0],
        ),
        reverse=True,
    )[:limit]
    return dict(ranked)
