"""Structured performance diagnostics for REQL commands."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Iterator

from .domain.timeutils import utcnow_iso


class PerformanceLogger:
    """Append-only JSONL profiler.

    Each line is independent JSON so logs remain readable after interruption.
    """

    def __init__(self, path: str | Path, *, command: str | None = None) -> None:
        self.path = Path(path).expanduser().resolve(strict=False)
        self.command = command
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.event("profile_start", category="lifecycle")

    def event(self, name: str, *, category: str = "event", **fields: Any) -> None:
        payload = {
            "ts": utcnow_iso(),
            "pid": os.getpid(),
            "thread": threading.current_thread().name,
            "category": category,
            "name": name,
            "command": self.command,
            **_jsonable_fields(fields),
        }
        with self._lock:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
                fh.write("\n")

    @contextmanager
    def span(self, name: str, *, category: str = "span", **fields: Any) -> Iterator[None]:
        start = time.perf_counter()
        ok = False
        try:
            yield
            ok = True
        finally:
            self.event(
                name,
                category=category,
                duration_ms=round((time.perf_counter() - start) * 1000.0, 3),
                ok=ok,
                **fields,
            )


def _jsonable_fields(fields: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in fields.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = value
        elif isinstance(value, Path):
            out[key] = str(value)
        elif isinstance(value, (list, tuple, set)):
            out[key] = [str(item) if isinstance(item, Path) else item for item in value]
        elif isinstance(value, dict):
            out[key] = _jsonable_fields(value)
        else:
            out[key] = str(value)
    return out
