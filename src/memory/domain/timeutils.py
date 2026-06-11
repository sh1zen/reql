"""Time helpers."""
from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    return utcnow().isoformat()


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        if value.endswith("Z"):
            dt = datetime.fromisoformat(value[:-1] + "+00:00")
        else:
            raise
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def seconds_since(value: str | None, now: datetime | None = None) -> float:
    dt = parse_dt(value)
    if dt is None:
        return 0.0
    now = now or utcnow()
    return max(0.0, (now - dt).total_seconds())
