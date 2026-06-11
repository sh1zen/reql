"""Lightweight signature utilities shared by ingest and linking."""
from __future__ import annotations

from collections import Counter
from typing import Any


def sparse_signature_vector(signature: dict[str, Any]) -> Counter[str]:
    """Build a sparse deterministic vector from an extraction signature."""

    items: list[str] = []
    items.extend(f"tok:{item}" for item in signature.get("normalized_tokens", []) if isinstance(item, str))
    items.extend(f"topic:{item}" for item in signature.get("main_topics", []) if isinstance(item, str))
    items.extend(f"entity:{item}" for item in signature.get("entity_canonical_keys", []) if isinstance(item, str))
    items.extend(f"pred:{item}" for item in signature.get("relation_predicates", []) if isinstance(item, str))
    return Counter(items)
