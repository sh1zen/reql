"""Text normalization and deterministic lexical utilities."""
from __future__ import annotations

import re
import unicodedata
from collections import Counter

_WORD_RE = re.compile(r"[\w\u00C0-\u017F][\w\u00C0-\u017F'\-]{1,}", re.UNICODE)
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n+")


def strip_accents(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def normalize_text(value: str) -> str:
    value = value.strip().replace("\u2019", "'").replace("\u2018", "'")
    value = value.replace("\u201c", '"').replace("\u201d", '"')
    value = re.sub(r"\s+", " ", value)
    return value


def canonicalize(value: str) -> str:
    value = normalize_text(value).lower()
    value = strip_accents(value)
    value = re.sub(r"[^a-z0-9_\-\s]", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def token_signal_score(token: str) -> float:
    """Estimate token usefulness without language-specific stopword lists."""
    token = token.strip("_-")
    if len(token) < 2:
        return 0.0
    has_alpha = any(char.isalpha() for char in token)
    has_digit = any(char.isdigit() for char in token)
    has_structure = any(separator in token for separator in ("_", "-", ".", "/", "\\", "#", "+"))
    if not has_alpha and not has_digit:
        return 0.0
    score = 0.25
    if len(token) >= 4:
        score += 0.25
    if len(token) >= 6:
        score += 0.25
    if has_digit:
        score += 0.15
    if has_structure:
        score += 0.20
    return min(1.0, score)


def tokenize(value: str, *, keep_stopwords: bool = False) -> list[str]:
    canonical = canonicalize(value)
    tokens = [m.group(0).strip("_-") for m in _WORD_RE.finditer(canonical)]
    tokens = [t for t in tokens if len(t) >= 2]
    if not keep_stopwords:
        tokens = [t for t in tokens if token_signal_score(t) > 0.0]
    return tokens


def split_sentences(value: str) -> list[str]:
    value = normalize_text(value)
    pieces = [p.strip() for p in _SENTENCE_RE.split(value) if p.strip()]
    # Split very long comma-heavy statements into semantically useful chunks.
    out: list[str] = []
    for piece in pieces:
        if len(piece) > 260 and ";" in piece:
            out.extend([p.strip() for p in piece.split(";") if p.strip()])
        else:
            out.append(piece)
    return out or ([value] if value else [])


def keyword_scores(value: str, *, max_terms: int = 12) -> list[tuple[str, float]]:
    tokens = tokenize(value)
    if not tokens:
        return []
    counts = Counter(tokens)
    # Add compact bigrams when both sides have useful structural signal.
    for a, b in zip(tokens, tokens[1:]):
        if token_signal_score(a) >= 0.5 and token_signal_score(b) >= 0.5:
            counts[f"{a} {b}"] += 1.25
    max_count = max(counts.values()) or 1
    scored = []
    for term, count in counts.items():
        if " " in term:
            specificity = 1.15 + min(token_signal_score(part) for part in term.split())
        else:
            specificity = token_signal_score(term)
        scored.append((term, min(1.0, (count / max_count) * specificity)))
    scored.sort(key=lambda item: (item[1], len(item[0])), reverse=True)
    return scored[:max_terms]


def extract_capitalized_entities(value: str) -> list[str]:
    candidates: list[str] = []
    # Quoted strings and code-like tokens carry useful entity signal without a language list.
    for quoted in re.findall(r"['\"]([^'\"]{2,60})['\"]", value):
        candidates.append(quoted.strip())
    for token in re.findall(r"\b[A-Z\u00C0-\u017F][\w\u00C0-\u017F0-9+#.-]{1,}\b", value):
        if _looks_entity_like(token):
            candidates.append(token)
    for token in re.findall(r"\b(?:[A-Z]{2,}|[A-Za-z]+\d[A-Za-z0-9]*)\b", value):
        if _looks_entity_like(token):
            candidates.append(token)
    seen = set()
    out = []
    for candidate in candidates:
        key = canonicalize(candidate)
        if key and key not in seen:
            seen.add(key)
            out.append(candidate.strip())
    return out


def _looks_entity_like(token: str) -> bool:
    if len(token) < 3:
        return False
    if any(separator in token for separator in ("_", "-", ".", "/", "\\", "#", "+")):
        return True
    if any(char.isdigit() for char in token):
        return True
    if token.isupper() and len(token) >= 2:
        return True
    return any(char.islower() for char in token) and any(char.isupper() for char in token[1:])


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def short_label(value: str, max_len: int = 72) -> str:
    value = normalize_text(value)
    return value if len(value) <= max_len else value[: max_len - 1].rstrip() + "..."
