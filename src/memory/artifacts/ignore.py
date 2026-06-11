"""Default path filtering for project file walks."""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path

DEFAULT_IGNORE_PATTERNS = (
    ".git/",
    ".reql/",
    ".venv/",
    "venv/",
    "node_modules/",
    "dist/",
    "build/",
    "__pycache__/",
    ".mypy_cache/",
    ".pytest_cache/",
    ".ruff_cache/",
    ".DS_Store",
    "*.pyc",
    "*.sqlite",
    "*.sqlite-journal",
    "*.sqlite-shm",
    "*.sqlite-wal",
    "*.sqlite3",
    "*.sqlite3-journal",
    "*.sqlite3-shm",
    "*.sqlite3-wal",
    "*.db",
    "*.db-journal",
    "*.db-shm",
    "*.db-wal",
)


@dataclass(frozen=True, slots=True)
class IgnoreRule:
    pattern: str
    negated: bool = False
    directory_only: bool = False
    anchored: bool = False

    @classmethod
    def parse(cls, raw: str) -> "IgnoreRule | None":
        line = raw.strip()
        if not line or line.startswith("#"):
            return None
        negated = line.startswith("!")
        if negated:
            line = line[1:].strip()
        if not line:
            return None
        directory_only = line.endswith("/")
        if directory_only:
            line = line.rstrip("/")
        anchored = line.startswith("/")
        line = line.lstrip("/").replace("\\", "/")
        if not line:
            return None
        return cls(line, negated=negated, directory_only=directory_only, anchored=anchored)

    def matches(self, relative_path: str, *, is_dir: bool) -> bool:
        rel = relative_path.replace("\\", "/").strip("/")
        if not rel:
            return False
        if self.directory_only and not (is_dir or _contains_dir(rel, self.pattern)):
            return False
        if self.anchored:
            return _match_path(rel, self.pattern, directory_only=self.directory_only)
        if _match_path(rel, self.pattern, directory_only=self.directory_only):
            return True
        parts = rel.split("/")
        if "/" not in self.pattern:
            return any(fnmatch.fnmatchcase(part, self.pattern) for part in parts)
        return False


class IgnoreMatcher:
    def __init__(self, rules: list[IgnoreRule]) -> None:
        self.rules = rules

    def is_ignored(self, relative_path: str, *, is_dir: bool = False) -> bool:
        ignored = False
        for rule in self.rules:
            if rule.matches(relative_path, is_dir=is_dir):
                ignored = not rule.negated
        return ignored


def build_ignore_matcher(
    root: str | Path,
    *,
    use_default_ignores: bool = True,
) -> IgnoreMatcher:
    """Build a matcher from built-in default rules."""

    rules: list[IgnoreRule] = []
    if use_default_ignores:
        rules.extend(rule for pattern in DEFAULT_IGNORE_PATTERNS if (rule := IgnoreRule.parse(pattern)))
    return IgnoreMatcher(rules)


def _match_path(relative_path: str, pattern: str, *, directory_only: bool) -> bool:
    if fnmatch.fnmatchcase(relative_path, pattern):
        return True
    if directory_only:
        return relative_path == pattern or relative_path.startswith(pattern + "/") or ("/" + pattern + "/") in ("/" + relative_path + "/")
    if "/" in pattern:
        return fnmatch.fnmatchcase(relative_path, pattern) or fnmatch.fnmatchcase(relative_path, "*/" + pattern)
    return False


def _contains_dir(relative_path: str, pattern: str) -> bool:
    if "/" in pattern:
        return relative_path == pattern or relative_path.startswith(pattern + "/") or ("/" + pattern + "/") in ("/" + relative_path + "/")
    return pattern in relative_path.split("/")
