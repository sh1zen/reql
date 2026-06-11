"""Structured performance diagnostics for REQL commands."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Iterator

from .domain.timeutils import utcnow_iso


@dataclass(frozen=True, slots=True)
class ProfileSummary:
    path: str
    events: int
    total_duration_ms: float
    by_name: list[dict[str, Any]]
    slowest: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "events": self.events,
            "total_duration_ms": self.total_duration_ms,
            "by_name": self.by_name,
            "slowest": self.slowest,
        }


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


def summarize_profile_log(path: str | Path, *, limit: int = 20) -> ProfileSummary:
    log_path = Path(path).expanduser().resolve(strict=False)
    events: list[dict[str, Any]] = []
    if not log_path.exists():
        return ProfileSummary(path=str(log_path), events=0, total_duration_ms=0.0, by_name=[], slowest=[])

    with log_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                events.append(item)

    grouped: dict[str, dict[str, Any]] = {}
    total = 0.0
    for item in events:
        duration = _duration(item)
        if duration <= 0:
            continue
        total += duration
        name = str(item.get("name") or "unknown")
        row = grouped.setdefault(name, {"name": name, "count": 0, "total_duration_ms": 0.0, "max_duration_ms": 0.0})
        row["count"] += 1
        row["total_duration_ms"] = round(float(row["total_duration_ms"]) + duration, 3)
        row["max_duration_ms"] = max(float(row["max_duration_ms"]), duration)

    by_name = sorted(grouped.values(), key=lambda row: (float(row["total_duration_ms"]), int(row["count"])), reverse=True)
    slowest = sorted(
        (
            {
                "name": str(item.get("name") or "unknown"),
                "category": str(item.get("category") or ""),
                "duration_ms": _duration(item),
                "fields": {key: value for key, value in item.items() if key not in {"ts", "pid", "thread", "category", "name", "command", "duration_ms"}},
            }
            for item in events
            if _duration(item) > 0
        ),
        key=lambda item: float(item["duration_ms"]),
        reverse=True,
    )[:limit]
    return ProfileSummary(path=str(log_path), events=len(events), total_duration_ms=round(total, 3), by_name=by_name[:limit], slowest=slowest)


def _duration(item: dict[str, Any]) -> float:
    try:
        return float(item.get("duration_ms", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


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
