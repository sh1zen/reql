"""Project artifact scanning and graph registration."""
from __future__ import annotations

from typing import Any

from .models import (
    ArtifactFingerprint,
    GraphRegistrationSummary,
    Project,
    ScanError,
    ScanResult,
    ScanSkippedFile,
    SourceArtifact,
)

__all__ = [
    "ArtifactFingerprint",
    "ArtifactCache",
    "ArtifactCacheEntry",
    "artifact_cache_path",
    "ArtifactCompilationResult",
    "ArtifactCompiler",
    "archive_artifact_fragments",
    "CompilationRun",
    "DeltaRepository",
    "DirtySet",
    "GraphRegistrationSummary",
    "GraphDelta",
    "Project",
    "ProjectRegistry",
    "ProjectScanner",
    "ScanError",
    "ScanResult",
    "ScanSkippedFile",
    "SourceArtifact",
]


def __getattr__(name: str) -> Any:
    if name in {"ArtifactCache", "ArtifactCacheEntry", "DirtySet", "artifact_cache_path"}:
        from . import cache

        return getattr(cache, name)
    if name in {"ArtifactCompiler", "ArtifactCompilationResult", "archive_artifact_fragments"}:
        from . import compiler

        return getattr(compiler, name)
    if name in {"CompilationRun", "DeltaRepository", "GraphDelta"}:
        from . import delta

        return getattr(delta, name)
    if name == "ProjectRegistry":
        from .project import ProjectRegistry

        return ProjectRegistry
    if name == "ProjectScanner":
        from .scanner import ProjectScanner

        return ProjectScanner
    raise AttributeError(name)
