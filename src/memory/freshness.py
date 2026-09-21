"""Atomic project-watcher state used to disclose graph freshness."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from .domain.timeutils import utcnow_iso


def watch_state_path(storage_path: str | Path) -> Path:
    path = Path(storage_path)
    return path.with_name(f"{path.name}.watch.json")


def read_watch_state(storage_path: str | Path) -> dict[str, Any]:
    path = watch_state_path(storage_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write_watch_state(storage_path: str | Path, **values: Any) -> dict[str, Any]:
    path = watch_state_path(storage_path)
    payload = {"format": "reql-watch-state-v1", "checked_at": utcnow_iso(), **values}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return payload
