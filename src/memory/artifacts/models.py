"""Domain objects for scanned projects and source artifacts."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal

from ..domain.timeutils import utcnow_iso

ArtifactType = Literal[
    "code",
    "markdown",
    "text",
    "pdf",
    "config",
    "data",
    "binary",
    "unknown",
]


@dataclass(slots=True)
class Project:
    id: str
    root_path: str
    name: str
    status: str = "active"
    created_at: str = field(default_factory=utcnow_iso)
    updated_at: str = field(default_factory=utcnow_iso)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class SourceArtifact:
    id: str
    project_id: str
    uri: str
    path: str
    relative_path: str
    artifact_type: ArtifactType
    language: str | None
    size_bytes: int
    sha256: str
    mtime: float
    status: str = "active"
    created_at: str = field(default_factory=utcnow_iso)
    updated_at: str = field(default_factory=utcnow_iso)
    last_seen_at: str = field(default_factory=utcnow_iso)
    last_compiled_at: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ArtifactFingerprint:
    path: str
    relative_path: str
    size_bytes: int
    mtime: float
    sha256: str
    parser_version: str
    chunking_version: str
    options_hash: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ScanSkippedFile:
    path: str
    relative_path: str
    reason: str
    size_bytes: int | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ScanError:
    path: str
    relative_path: str
    message: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class GraphRegistrationSummary:
    project_created: bool = False
    artifacts_created: int = 0
    artifacts_updated: int = 0
    artifacts_archived: int = 0
    edges_created: int = 0
    edges_updated: int = 0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class ScanResult:
    project: Project
    artifacts: list[SourceArtifact]
    skipped_files: list[ScanSkippedFile]
    errors: list[ScanError]
    counts_by_type: dict[str, int]
    registration: GraphRegistrationSummary | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "project": self.project.to_dict(),
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
            "skipped_files": [item.to_dict() for item in self.skipped_files],
            "errors": [item.to_dict() for item in self.errors],
            "counts_by_type": dict(self.counts_by_type),
            "registration": self.registration.to_dict() if self.registration else None,
        }
