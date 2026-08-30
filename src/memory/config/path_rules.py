"""Strict, scope-aware path rules for configured scan exclusions."""
from __future__ import annotations

from dataclasses import dataclass


_FORBIDDEN_GLOB_CHARS = frozenset("?[]{}")


@dataclass(frozen=True, slots=True)
class ScanExcludeRule:
    """One validated exclusion rule resolved relative to a config directory."""

    anchored: bool
    segments: tuple[str, ...]
    wildcard_suffix: str | None = None

    @property
    def normalized(self) -> str:
        prefix = "./" if self.anchored else ""
        return prefix + "/".join(self.segments)

    def matches(self, relative_path: str) -> bool:
        """Return whether a config-relative candidate is selected by this rule."""

        candidate = tuple(part for part in relative_path.replace("\\", "/").strip("/").split("/") if part)
        if self.wildcard_suffix is not None:
            if not candidate or not candidate[-1].endswith(self.wildcard_suffix):
                return False
            directory_selector = self.segments[:-1]
            candidate_directories = candidate[:-1]
            if self.anchored:
                if not directory_selector:
                    return len(candidate) == 1
                return candidate_directories[: len(directory_selector)] == directory_selector
            if not directory_selector:
                return True
            selector_size = len(directory_selector)
            return any(
                candidate_directories[index : index + selector_size] == directory_selector
                for index in range(len(candidate_directories) - selector_size + 1)
            )
        if len(candidate) != len(self.segments) and self.anchored:
            return False
        if len(candidate) < len(self.segments):
            return False
        compared = candidate if self.anchored else candidate[-len(self.segments) :]
        for actual, expected in zip(compared, self.segments):
            if actual != expected:
                return False
        return True


def resolve_scan_exclude_pattern(pattern: str) -> ScanExcludeRule:
    """Parse the one supported exclusion grammar into a reusable matcher.

    Rules are relative paths with an optional ``./`` anchor and optional trailing
    slash. A wildcard is accepted only at the start of the final segment and
    must have a non-empty suffix, for example ``*.pyc`` or ``dir/*generated``.
    """

    if not isinstance(pattern, str):
        raise ValueError("scan.exclude patterns must be strings")
    if not pattern or pattern != pattern.strip():
        raise _invalid_pattern(pattern, "leading/trailing whitespace is not allowed")
    if "\\" in pattern:
        raise _invalid_pattern(pattern, "use '/' as the path separator")
    if "\n" in pattern or "\r" in pattern or "\0" in pattern:
        raise _invalid_pattern(pattern, "control characters are not allowed")

    anchored = pattern.startswith("./")
    raw_path = pattern[2:] if anchored else pattern
    if raw_path.startswith("/"):
        raise _invalid_pattern(pattern, "absolute paths are not allowed")
    if len(raw_path) >= 2 and raw_path[1] == ":":
        raise _invalid_pattern(pattern, "drive-qualified paths are not allowed")
    if raw_path.endswith("/"):
        raw_path = raw_path[:-1]
    if not raw_path:
        raise _invalid_pattern(pattern, "a file or directory name is required")

    segments = tuple(raw_path.split("/"))
    if any(not segment for segment in segments):
        raise _invalid_pattern(pattern, "empty path segments are not allowed")
    if any(segment in {".", ".."} for segment in segments):
        raise _invalid_pattern(pattern, "'.' and '..' path segments are not allowed")
    if any(any(char in segment for char in _FORBIDDEN_GLOB_CHARS) for segment in segments):
        raise _invalid_pattern(pattern, "only a leading '*' in the final segment is supported")

    wildcard_suffix: str | None = None
    starred = [index for index, segment in enumerate(segments) if "*" in segment]
    if starred:
        if starred != [len(segments) - 1]:
            raise _invalid_pattern(pattern, "'*' is allowed only in the final path segment")
        wildcard_segment = segments[-1]
        if not wildcard_segment.startswith("*") or wildcard_segment.count("*") != 1:
            raise _invalid_pattern(pattern, "the final wildcard segment must use the form '*suffix'")
        wildcard_suffix = wildcard_segment[1:]
        if not wildcard_suffix:
            raise _invalid_pattern(pattern, "the wildcard suffix must not be empty")

    return ScanExcludeRule(anchored=anchored, segments=segments, wildcard_suffix=wildcard_suffix)


def normalize_scan_exclude_pattern(pattern: str) -> str:
    """Return the canonical key while preserving anchored rule semantics."""

    return resolve_scan_exclude_pattern(pattern).normalized


def _invalid_pattern(pattern: str, reason: str) -> ValueError:
    return ValueError(f"Invalid scan.exclude pattern {pattern!r}: {reason}")
