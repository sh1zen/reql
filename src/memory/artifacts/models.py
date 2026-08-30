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


@dataclass(slots=True)
class ArtifactCompilationResult:
    """Deduplicated graph changes produced while compiling artifacts."""

    artifact_id: str
    added_nodes: set[str] = field(default_factory=set)
    updated_nodes: set[str] = field(default_factory=set)
    archived_nodes: set[str] = field(default_factory=set)
    added_edges: set[str] = field(default_factory=set)
    updated_edges: set[str] = field(default_factory=set)
    archived_edges: set[str] = field(default_factory=set)
    affected_node_ids: set[str] = field(default_factory=set)
    affected_edge_ids: set[str] = field(default_factory=set)
    affected_community_ids: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)

    def merge(self, *results: "ArtifactCompilationResult") -> "ArtifactCompilationResult":
        """Merge child phases without introducing a parallel aggregate model."""
        for result in results:
            self.added_nodes.update(result.added_nodes)
            self.updated_nodes.update(result.updated_nodes)
            self.archived_nodes.update(result.archived_nodes)
            self.added_edges.update(result.added_edges)
            self.updated_edges.update(result.updated_edges)
            self.archived_edges.update(result.archived_edges)
            self.affected_node_ids.update(result.affected_node_ids)
            self.affected_edge_ids.update(result.affected_edge_ids)
            self.affected_community_ids.update(result.affected_community_ids)
            self.errors.extend(result.errors)
        return self

    def record_node(self, node_id: str, *, created: bool) -> None:
        (self.added_nodes if created else self.updated_nodes).add(node_id)
        self.affected_node_ids.add(node_id)

    def record_edge(self, edge_id: str, *, created: bool) -> None:
        (self.added_edges if created else self.updated_edges).add(edge_id)
        self.affected_edge_ids.add(edge_id)
