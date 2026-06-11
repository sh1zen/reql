"""Project artifact scanning and graph registration."""
from __future__ import annotations

from .models import (
    ArtifactFingerprint,
    GraphRegistrationSummary,
    Project,
    ScanError,
    ScanResult,
    ScanSkippedFile,
    SourceArtifact,
)
from .cache import ArtifactCache, ArtifactCacheEntry, DirtySet, artifact_cache_path
from .compiler import ArtifactCompiler, ArtifactCompilationResult, archive_artifact_fragments
from .delta import CompilationRun, DeltaRepository, GraphDelta
from .project import ProjectRegistry
from .scanner import ProjectScanner

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
