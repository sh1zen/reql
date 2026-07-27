"""Recall-oriented lexical indexing for the block graph store.

The persisted block format stays unchanged.  This adapter only changes how
lexical postings are derived from node text, and carries a small schema marker
inside the root index so stores created with the older head-only index are
rebuilt transparently on first use.
"""
from __future__ import annotations

from collections import Counter
import hashlib
from typing import Any, Iterable, Sequence

from ...domain.models import MemoryNode
from ...extraction.normalization import keyword_scores, token_signal_score, tokenize
from .block_store import INDEXED_NODE_PROPERTIES, BlockGraphStore as _BaseBlockGraphStore

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


class BlockGraphStore(_BaseBlockGraphStore):
    """Block graph store with higher-recall, bounded lexical postings."""

    def _build_root_index(
        self,
        *,
        locations: dict[str, dict[str, dict[str, int]]],
        space_map: dict[str, Any],
        generation_id: int,
    ) -> dict[str, Any]:
        root = super()._build_root_index(
            locations=locations,
            space_map=space_map,
            generation_id=generation_id,
        )
        root["lexical_schema_version"] = LEXICAL_INDEX_SCHEMA_VERSION
        return root

    def _apply_root_index(self, value: dict[str, Any]) -> None:
        try:
            loaded_version = int(value.get("lexical_schema_version", 1) or 1)
        except (TypeError, ValueError):
            loaded_version = 1
        self._loaded_lexical_schema_version = loaded_version
        super()._apply_root_index(value)

    def _ensure_lexical_index_loaded(self) -> None:
        super()._ensure_lexical_index_loaded()
        loaded_version = int(
            getattr(self, "_loaded_lexical_schema_version", LEXICAL_INDEX_SCHEMA_VERSION)
        )
        if loaded_version >= LEXICAL_INDEX_SCHEMA_VERSION:
            return

        # Old stores only indexed the first 4 KiB and kept at most 80 terms per
        # node.  Materialize every node before rebuilding so a cold persisted
        # store receives the same index as a newly compiled one.
        self._materialize_all_records()
        self._rebuild_lexical_index()
        self._loaded_lexical_schema_version = LEXICAL_INDEX_SCHEMA_VERSION
        if not self.read_only:
            self._dirty = True

    def _node_lexical_fingerprint(self, node: MemoryNode) -> tuple[str, ...]:
        digest = hashlib.blake2b(digest_size=20)
        for value in self._fingerprint_values(node):
            encoded = value.encode("utf-8", errors="replace")
            digest.update(len(encoded).to_bytes(8, "little", signed=False))
            digest.update(encoded)
        return (str(LEXICAL_INDEX_SCHEMA_VERSION), digest.hexdigest())

    def _reindex_node_terms(self, node: MemoryNode) -> None:
        text_terms = _text_term_weights(node.text or "")
        metadata_terms = _metadata_term_weights(node, self._property_values(node))

        combined: dict[str, float] = dict(text_terms)
        for term, weight in metadata_terms.items():
            combined[term] = max(combined.get(term, 0.0), weight)
        combined = _cap_terms(combined, LEXICAL_MAX_TERMS_PER_NODE)

        node_terms: set[str] = set()
        for term, score in combined.items():
            self._node_terms[term][node.id] = float(score)
            node_terms.add(term)

        self._node_lexical_fingerprints[node.id] = self._node_lexical_fingerprint(node)
        if node_terms:
            self._node_term_index[node.id] = node_terms
        else:
            self._node_term_index.pop(node.id, None)

    @staticmethod
    def _property_values(node: MemoryNode) -> list[str]:
        values: list[str] = []
        remaining = LEXICAL_PROPERTY_VALUE_LIMIT
        for key in sorted(INDEXED_NODE_PROPERTIES):
            if remaining <= 0:
                break
            value = node.properties.get(key)
            if isinstance(value, (str, int, float, bool)):
                values.append(str(value)[:512])
                remaining -= 1
                continue
            if isinstance(value, (list, tuple, set, frozenset)):
                items: Iterable[Any]
                if isinstance(value, (set, frozenset)):
                    items = sorted(value, key=str)
                else:
                    items = value
                for item in items:
                    if remaining <= 0:
                        break
                    if isinstance(item, (str, int, float, bool)):
                        values.append(str(item)[:256])
                        remaining -= 1
        return values

    def _fingerprint_values(self, node: MemoryNode) -> Iterable[str]:
        yield node.type
        yield node.label or ""
        yield node.canonical_key or ""
        yield node.text or ""
        yield from self._property_values(node)


def _text_term_weights(value: str) -> dict[str, float]:
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
    values = [node.type, node.label or "", node.canonical_key or "", *property_values]
    text = " ".join(value for value in values if value)
    if not text:
        return {}
    weights = _selected_token_weights(text, LEXICAL_METADATA_TOKEN_BUDGET)
    for term, score in keyword_scores(text, max_terms=LEXICAL_METADATA_KEYWORD_BUDGET):
        weights[term] = max(weights.get(term, 0.0), float(score))
    return _cap_terms(weights, LEXICAL_METADATA_TERM_BUDGET)


def _selected_token_weights(value: str, limit: int) -> dict[str, float]:
    tokens = tokenize(value)
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
    signal = token_signal_score(token)
    rarity = 1.0 / max(1.0, float(count) ** 0.5)
    return min(1.0, 0.30 + (0.55 * signal) + (0.15 * rarity))


def _text_windows(value: str) -> list[str]:
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


__all__ = ["BlockGraphStore"]
