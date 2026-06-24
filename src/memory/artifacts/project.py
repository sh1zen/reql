"""Graph integration for scanned projects and source artifacts."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..domain.ids import stable_id
from ..domain.models import MemoryEdge, MemoryNode
from ..domain.timeutils import utcnow_iso
from ..storage.graph_store import GraphStore
from .context_scope import artifact_context_scope
from .fingerprint import normalize_path, project_id
from .models import GraphRegistrationSummary, Project, ScanResult, SourceArtifact
from .scanner import DEFAULT_MAX_FILE_SIZE_BYTES, ProjectScanner


class ProjectRegistry:
    """Creates and updates project/artifact graph records from scan results."""

    def __init__(self, store: GraphStore) -> None:
        self.store = store

    def scan_path(
        self,
        path: str | Path,
        *,

        max_file_size_bytes: int = DEFAULT_MAX_FILE_SIZE_BYTES,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
    ) -> ScanResult:
        scanner = ProjectScanner(
            max_file_size_bytes=max_file_size_bytes,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
        )
        result = scanner.scan(path)
        result.registration = self.register_scan(result)
        return result

    def register_scan(self, result: ScanResult) -> GraphRegistrationSummary:
        summary = GraphRegistrationSummary()
        project_node, project_created = self.store.upsert_node(_project_node(result.project))
        summary.project_created = project_created
        seen_paths: set[str] = set()

        for artifact in result.artifacts:
            seen_paths.add(artifact.relative_path)
            parent_id = project_node.id
            directory_parts = Path(artifact.relative_path).parent.parts
            current_relative = ""
            for part in directory_parts:
                if part in {"", "."}:
                    continue
                current_relative = f"{current_relative}/{part}".strip("/")
                directory_node, directory_created = self.store.upsert_node(_directory_node(result.project, current_relative))
                if directory_created:
                    summary.artifacts_created += 1
                else:
                    summary.artifacts_updated += 1
                _, edge_created = self.store.upsert_edge(
                    _technical_edge(
                        parent_id,
                        directory_node.id,
                        "CONTAINS",
                        source_file=current_relative,
                        line_start=None,
                        line_end=None,
                        extractor="project_scanner",
                        evidence=current_relative,
                        properties={"project_id": result.project.id, "relative_path": current_relative, "target_kind": "directory"},
                    )
                )
                if edge_created:
                    summary.edges_created += 1
                else:
                    summary.edges_updated += 1
                parent_id = directory_node.id

            file_node, file_created = self.store.upsert_node(_file_node(artifact))
            if file_created:
                summary.artifacts_created += 1
            else:
                summary.artifacts_updated += 1
            _, edge_created = self.store.upsert_edge(
                _technical_edge(
                    parent_id,
                    file_node.id,
                    "CONTAINS",
                    source_file=artifact.relative_path,
                    line_start=None,
                    line_end=None,
                    extractor="project_scanner",
                    evidence=artifact.relative_path,
                    properties={"project_id": artifact.project_id, "artifact_id": artifact.id, "relative_path": artifact.relative_path, "target_kind": "file"},
                )
            )
            if edge_created:
                summary.edges_created += 1
            else:
                summary.edges_updated += 1

            artifact_node, created = self.store.upsert_node(_artifact_node(artifact))
            if created:
                summary.artifacts_created += 1
            else:
                summary.artifacts_updated += 1
            _, edge_created = self.store.upsert_edge(_contains_edge(file_node.id, artifact_node.id, artifact.relative_path, artifact.project_id))
            if edge_created:
                summary.edges_created += 1
            else:
                summary.edges_updated += 1
            if artifact.relative_path.replace("\\", "/").endswith("/__init__.py") or artifact.relative_path == "__init__.py":
                package_node, package_created = self.store.upsert_node(_package_node(artifact))
                if package_created:
                    summary.artifacts_created += 1
                else:
                    summary.artifacts_updated += 1
                _, edge_created = self.store.upsert_edge(
                    _technical_edge(
                            parent_id,
                        package_node.id,
                        "DEFINES",
                        source_file=artifact.relative_path,
                        line_start=1,
                        line_end=1,
                        extractor="project_scanner",
                        evidence=artifact.relative_path,
                        properties={"project_id": artifact.project_id, "artifact_id": artifact.id, "relative_path": artifact.relative_path, "kind": "package"},
                    )
                )
                if edge_created:
                    summary.edges_created += 1
                else:
                    summary.edges_updated += 1

        summary.artifacts_archived = self._archive_missing(result.project, seen_paths)
        return summary

    def register_scan_delta(self, result: ScanResult, changed_artifact_ids: set[str]) -> GraphRegistrationSummary:
        """Register only changed artifacts from a scan.

        Incremental compilation computes the dirty set before graph writes. This
        method keeps the project graph complete for changed files while avoiding
        the expensive full project rewrite on no-op and small-delta compiles.
        """
        summary = GraphRegistrationSummary()
        project_node, project_created = self.store.upsert_node(_project_node(result.project))
        summary.project_created = project_created
        directory_ids: dict[str, str] = {}

        for artifact in result.artifacts:
            if artifact.id not in changed_artifact_ids:
                continue
            parent_id = project_node.id
            directory_parts = Path(artifact.relative_path).parent.parts
            current_relative = ""
            for part in directory_parts:
                if part in {"", "."}:
                    continue
                current_relative = f"{current_relative}/{part}".strip("/")
                directory_node = self.store.get_node(directory_ids[current_relative]) if current_relative in directory_ids else None
                if directory_node is None:
                    directory_node, directory_created = self.store.upsert_node(_directory_node(result.project, current_relative))
                    directory_ids[current_relative] = directory_node.id
                    if directory_created:
                        summary.artifacts_created += 1
                    else:
                        summary.artifacts_updated += 1
                _, edge_created = self.store.upsert_edge(
                    _technical_edge(
                        parent_id,
                        directory_node.id,
                        "CONTAINS",
                        source_file=current_relative,
                        line_start=None,
                        line_end=None,
                        extractor="project_scanner",
                        evidence=current_relative,
                        properties={"project_id": result.project.id, "relative_path": current_relative, "target_kind": "directory"},
                    )
                )
                if edge_created:
                    summary.edges_created += 1
                else:
                    summary.edges_updated += 1
                parent_id = directory_node.id

            file_node, file_created = self.store.upsert_node(_file_node(artifact))
            if file_created:
                summary.artifacts_created += 1
            else:
                summary.artifacts_updated += 1
            _, edge_created = self.store.upsert_edge(
                _technical_edge(
                    parent_id,
                    file_node.id,
                    "CONTAINS",
                    source_file=artifact.relative_path,
                    line_start=None,
                    line_end=None,
                    extractor="project_scanner",
                    evidence=artifact.relative_path,
                    properties={"project_id": artifact.project_id, "artifact_id": artifact.id, "relative_path": artifact.relative_path, "target_kind": "file"},
                )
            )
            if edge_created:
                summary.edges_created += 1
            else:
                summary.edges_updated += 1

            artifact_node, created = self.store.upsert_node(_artifact_node(artifact))
            if created:
                summary.artifacts_created += 1
            else:
                summary.artifacts_updated += 1
            _, edge_created = self.store.upsert_edge(_contains_edge(file_node.id, artifact_node.id, artifact.relative_path, artifact.project_id))
            if edge_created:
                summary.edges_created += 1
            else:
                summary.edges_updated += 1
            if artifact.relative_path.replace("\\", "/").endswith("/__init__.py") or artifact.relative_path == "__init__.py":
                package_node, package_created = self.store.upsert_node(_package_node(artifact))
                if package_created:
                    summary.artifacts_created += 1
                else:
                    summary.artifacts_updated += 1
                _, edge_created = self.store.upsert_edge(
                    _technical_edge(
                            parent_id,
                        package_node.id,
                        "DEFINES",
                        source_file=artifact.relative_path,
                        line_start=1,
                        line_end=1,
                        extractor="project_scanner",
                        evidence=artifact.relative_path,
                        properties={"project_id": artifact.project_id, "artifact_id": artifact.id, "relative_path": artifact.relative_path, "kind": "package"},
                    )
                )
                if edge_created:
                    summary.edges_created += 1
                else:
                    summary.edges_updated += 1
        return summary

    def project_status(self, path: str | Path) -> dict[str, Any] | None:
        root = normalize_path(path)
        project = self.store.get_node_by_key("Project", root)
        if project is None:
            return None
        artifacts = self.store.find_nodes_by_property("project_id", project.id, type_="SourceArtifact", limit=100000)
        counts_by_type: dict[str, int] = {}
        status_counts: dict[str, int] = {}
        for artifact in artifacts:
            artifact_type = str(artifact.properties.get("artifact_type") or "unknown")
            counts_by_type[artifact_type] = counts_by_type.get(artifact_type, 0) + 1
            status_counts[artifact.status] = status_counts.get(artifact.status, 0) + 1
        return {
            "project": project.to_dict(),
            "artifacts": len(artifacts),
            "counts_by_type": counts_by_type,
            "status_counts": status_counts,
        }

    def _archive_missing(self, project: Project, seen_paths: set[str]) -> int:
        now = utcnow_iso()
        archived = 0
        for node in self.store.find_nodes_by_property("project_id", project.id, type_="SourceArtifact", limit=100000):
            if node.properties.get("relative_path") in seen_paths:
                continue
            if node.status in {"archived", "deleted"}:
                continue
            properties = dict(node.properties)
            properties["status"] = "archived"
            properties["updated_at"] = now
            self.store.update_node_fields(node.id, status="archived", properties=properties)
            archived += 1
        return archived


def _project_node(project: Project) -> MemoryNode:
    return MemoryNode(
        id=project.id,

        type="Project",
        label=project.name,
        text=project.root_path,
        canonical_key=project.root_path,
        properties=project.to_dict(),
        salience=0.25,
        confidence=1.0,
        status=project.status,
    )


def _artifact_node(artifact: SourceArtifact) -> MemoryNode:
    properties = artifact.to_dict()
    properties.update({"context_scope": artifact_context_scope(artifact)})
    return MemoryNode(
        id=artifact.id,

        type="SourceArtifact",
        label=artifact.relative_path,
        text=artifact.uri,
        canonical_key=f"{artifact.project_id}:{artifact.relative_path}",
        properties=properties,
        salience=0.10,
        confidence=1.0,
        status=artifact.status,
    )


def _directory_node(project: Project, relative_path: str) -> MemoryNode:
    return MemoryNode(
        id=stable_id("directory", project.id, relative_path),

        type="Directory",
        label=relative_path or project.name,
        canonical_key=f"{project.id}:directory:{relative_path}",
        properties={"project_id": project.id, "relative_path": relative_path, "mode": "compile", "is_technical": True, "is_semantic": False},
        salience=0.10,
        confidence=1.0,
        status="active",
    )


def _file_node(artifact: SourceArtifact) -> MemoryNode:
    return MemoryNode(
        id=stable_id("file", artifact.project_id, artifact.relative_path),

        type="File",
        label=artifact.relative_path,
        text=artifact.uri,
        canonical_key=f"{artifact.project_id}:file:{artifact.relative_path}",
        properties={
            "project_id": artifact.project_id,
            "artifact_id": artifact.id,
            "path": artifact.path,
            "relative_path": artifact.relative_path,
            "context_scope": artifact_context_scope(artifact),
            "artifact_type": artifact.artifact_type,
            "language": artifact.language,
            "sha256": artifact.sha256,
            "size_bytes": artifact.size_bytes,
            "mode": "compile",
            "is_technical": True,
            "is_semantic": False,
        },
        salience=0.12,
        confidence=1.0,
        status=artifact.status,
    )


def _package_node(artifact: SourceArtifact) -> MemoryNode:
    package = Path(artifact.relative_path).parent.as_posix().replace("/", ".")
    package = package if package != "." else Path(artifact.relative_path).parent.name or Path(artifact.relative_path).stem
    return MemoryNode(
        id=stable_id("package", artifact.project_id, package),

        type="Package",
        label=package,
        canonical_key=f"{artifact.project_id}:package:{package}",
        properties={
            "project_id": artifact.project_id,
            "artifact_id": artifact.id,
            "relative_path": artifact.relative_path,
            "context_scope": artifact_context_scope(artifact),
            "name": package,
            "mode": "compile",
            "is_technical": True,
            "is_semantic": False,
        },
        salience=0.14,
        confidence=1.0,
        status="active",
    )


def _contains_edge(file_id: str, artifact_id_: str, relative_path: str, project_id_: str) -> MemoryEdge:
    return _technical_edge(
        file_id,
        artifact_id_,
        "CONTAINS",
        source_file=relative_path,
        line_start=None,
        line_end=None,
        extractor="project_scanner",
        evidence=relative_path,
        properties={"project_id": project_id_, "relative_path": relative_path, "target_kind": "source_artifact"},
    )


def _technical_edge(
    from_id: str,
    to_id: str,
    type_: str,
    *,
    source_file: str,
    line_start: int | None,
    line_end: int | None,
    extractor: str,
    evidence: str,
    properties: dict[str, Any] | None = None,
) -> MemoryEdge:
    now = utcnow_iso()
    props = dict(properties or {})
    props.update(
        {
            "source_id": from_id,
            "target_id": to_id,
            "type": type_,
            "confidence": 1.0,
            "source_file": source_file,
            "line_start": line_start,
            "line_end": line_end,
            "extractor": extractor,
            "evidence": evidence,
            "created_at": now,
            "updated_at": now,
            "mode": "compile",
            "is_semantic": False,
            "is_technical": True,
        }
    )
    return MemoryEdge(
        id=stable_id("edge", from_id, type_, to_id),
        from_id=from_id,
        to_id=to_id,
        type=type_,
        weight=1.0,
        confidence=1.0,
        origin="deterministic",
        properties=props,
        created_at=now,
        updated_at=now,
    )
