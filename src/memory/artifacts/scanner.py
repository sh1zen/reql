"""Recursive project scanner for source artifact discovery."""
from __future__ import annotations

import os
from fnmatch import fnmatchcase
from pathlib import Path

from ..domain.timeutils import utcnow_iso
from .fingerprint import (
    DEFAULT_CHUNKING_VERSION,
    DEFAULT_PARSER_VERSION,
    artifact_id,
    file_uri,
    fingerprint_file,
    normalize_path,
    project_id,
    relative_path,
)
from .ignore import build_ignore_matcher
from .mime import classify_path, is_unsupported_media_file
from .models import Project, ScanError, ScanResult, ScanSkippedFile, SourceArtifact

DEFAULT_MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024
SAMPLE_BYTES = 8192


class ProjectScanner:
    """Scans a directory tree and returns project/artifact domain objects."""

    def __init__(
        self,
        *,
        max_file_size_bytes: int = DEFAULT_MAX_FILE_SIZE_BYTES,
        parser_version: str = DEFAULT_PARSER_VERSION,
        chunking_version: str = DEFAULT_CHUNKING_VERSION,
        options: dict[str, object] | None = None,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        use_default_ignores: bool = True,
    ) -> None:
        self.max_file_size_bytes = max_file_size_bytes
        self.parser_version = parser_version
        self.chunking_version = chunking_version
        self.options = options or {}
        self.include_patterns = include_patterns or []
        self.exclude_patterns = exclude_patterns or []
        self.use_default_ignores = use_default_ignores

    def scan(self, root_path: str | Path, *, name: str | None = None) -> ScanResult:
        root = Path(root_path).expanduser().resolve(strict=False)
        if not root.exists():
            raise FileNotFoundError(f"Project path does not exist: {root}")
        if not root.is_dir():
            raise NotADirectoryError(f"Project path is not a directory: {root}")

        now = utcnow_iso()
        normalized_root = normalize_path(root)
        project = Project(
            id=project_id(normalized_root),
            root_path=normalized_root,
            name=name or root.name or normalized_root,
            status="active",
            created_at=now,
            updated_at=now,
        )
        matcher = build_ignore_matcher(root, use_default_ignores=self.use_default_ignores)
        artifacts: list[SourceArtifact] = []
        skipped: list[ScanSkippedFile] = []
        errors: list[ScanError] = []

        stack = [root]
        while stack:
            current = stack.pop()
            try:
                entries = sorted(os.scandir(current), key=lambda item: item.name.casefold())
            except OSError as exc:
                rel = _safe_relative(root, current)
                errors.append(ScanError(normalize_path(current), rel, str(exc)))
                continue

            for entry in entries:
                path = Path(entry.path)
                rel = _safe_relative(root, path)
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                    is_file = entry.is_file(follow_symlinks=False)
                except OSError as exc:
                    errors.append(ScanError(normalize_path(path), rel, str(exc)))
                    continue

                if matcher.is_ignored(rel, is_dir=is_dir):
                    skipped.append(ScanSkippedFile(normalize_path(path), rel, "ignored"))
                    continue
                if _matches_any(rel, self.exclude_patterns, is_dir=is_dir):
                    skipped.append(ScanSkippedFile(normalize_path(path), rel, "excluded"))
                    continue
                if is_dir:
                    stack.append(path)
                    continue
                if not is_file:
                    skipped.append(ScanSkippedFile(normalize_path(path), rel, "not_regular_file"))
                    continue
                if self.include_patterns and not _matches_any(rel, self.include_patterns):
                    skipped.append(ScanSkippedFile(normalize_path(path), rel, "not_included"))
                    continue
                artifact = self._scan_file(root, path, project, now, skipped, errors)
                if artifact is not None:
                    artifacts.append(artifact)

        counts: dict[str, int] = {}
        for artifact in artifacts:
            counts[artifact.artifact_type] = counts.get(artifact.artifact_type, 0) + 1
        artifacts.sort(key=lambda item: item.relative_path)
        return ScanResult(project=project, artifacts=artifacts, skipped_files=skipped, errors=errors, counts_by_type=counts)

    def _scan_file(
        self,
        root: Path,
        path: Path,
        project: Project,
        now: str,
        skipped: list[ScanSkippedFile],
        errors: list[ScanError],
    ) -> SourceArtifact | None:
        rel = _safe_relative(root, path)
        try:
            stat = path.stat()
        except OSError as exc:
            errors.append(ScanError(normalize_path(path), rel, str(exc)))
            return None
        size = int(stat.st_size)
        if size > self.max_file_size_bytes:
            skipped.append(ScanSkippedFile(normalize_path(path), rel, "max_file_size_exceeded", size))
            return None
        try:
            with path.open("rb") as fh:
                sample = fh.read(SAMPLE_BYTES)
            fingerprint = fingerprint_file(
                root,
                path,
                parser_version=self.parser_version,
                chunking_version=self.chunking_version,
                options=self.options,
            )
        except OSError as exc:
            errors.append(ScanError(normalize_path(path), rel, str(exc)))
            return None
        if is_unsupported_media_file(path, sample):
            skipped.append(ScanSkippedFile(normalize_path(path), rel, "unsupported_media", size))
            return None
        classification = classify_path(path, sample)
        return SourceArtifact(
            id=artifact_id(project.id, fingerprint.relative_path),
            project_id=project.id,
            uri=file_uri(path),
            path=fingerprint.path,
            relative_path=fingerprint.relative_path,
            artifact_type=classification.artifact_type,
            language=classification.language,
            size_bytes=fingerprint.size_bytes,
            sha256=fingerprint.sha256,
            mtime=fingerprint.mtime,
            status="active",
            created_at=now,
            updated_at=now,
            last_seen_at=now,
            last_compiled_at=None,
        )


def _safe_relative(root: Path, path: Path) -> str:
    try:
        return relative_path(root, path)
    except ValueError:
        return Path(path).name


def _matches_any(relative: str, patterns: list[str], *, is_dir: bool = False) -> bool:
    rel = relative.replace("\\", "/")
    for pattern in patterns:
        normalized = pattern.replace("\\", "/")
        if normalized.endswith("/"):
            directory = normalized.rstrip("/")
            if rel == directory or rel.startswith(directory + "/") or ("/" + directory + "/") in ("/" + rel + "/"):
                return True
        if normalized.endswith("/**") and rel == normalized[:-3]:
            return True
        if normalized.startswith("**/"):
            tail = normalized[3:]
            if fnmatchcase(rel, tail):
                return True
        if fnmatchcase(rel, normalized):
            return True
        if is_dir and fnmatchcase(rel + "/", normalized):
            return True
    return False
