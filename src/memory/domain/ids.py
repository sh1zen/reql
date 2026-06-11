"""Stable id helpers."""
from __future__ import annotations

import hashlib
import uuid
from typing import Iterable


def new_id(prefix: str) -> str:
    return f"{prefix}:{uuid.uuid4().hex}"


def stable_hash(parts: Iterable[object], length: int = 24) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(str(part).encode("utf-8", errors="ignore"))
        h.update(b"\x1f")
    return h.hexdigest()[:length]


def stable_id(prefix: str, *parts: object) -> str:
    return f"{prefix}:{stable_hash(parts)}"
