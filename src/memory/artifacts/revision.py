"""Content-addressed project revision history for coding-agent context."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from ..domain.ids import stable_hash, stable_id
from ..domain.models import MemoryEdge, MemoryNode
from ..domain.timeutils import utcnow_iso
from ..storage.graph_store import GraphStore
from .models import SourceArtifact


@dataclass(frozen=True, slots=True)
class FileChange:
    """One path transition between a revision and its parent."""

    path: str
    status: str
    artifact_id: str
    old_sha256: str | None = None
    new_sha256: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class ProjectRevision:
    """Immutable, content-addressed snapshot metadata for a project tree."""

    id: str
    project_id: str
    run_id: str
    tree_hash: str
    sequence: int
    parent_id: str | None
    manifest: dict[str, dict[str, str]]
    changes: list[FileChange] = field(default_factory=list)
    created_at: str = field(default_factory=utcnow_iso)

    def to_dict(self, *, include_manifest: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": self.id,
            "project_id": self.project_id,
            "run_id": self.run_id,
            "tree_hash": self.tree_hash,
            "sequence": self.sequence,
            "parent_id": self.parent_id,
            "changes": [change.to_dict() for change in self.changes],
            "created_at": self.created_at,
        }
        if include_manifest:
            payload["manifest"] = {path: dict(value) for path, value in self.manifest.items()}
        return payload


class RevisionRepository:
    """Persist and retrieve Git-like project revisions in any graph store."""

    def __init__(self, store: GraphStore) -> None:
        self.store = store

    def record(
        self,
        *,
        project_id: str,
        run_id: str,
        artifacts: Iterable[SourceArtifact],
    ) -> ProjectRevision | None:
        manifest = _manifest(artifacts)
        parent = self.latest(project_id)
        old_manifest = parent.manifest if parent is not None else {}
        if parent is not None and manifest == old_manifest:
            return None

        changes = _changes(old_manifest, manifest)
        tree_hash = _tree_hash(manifest)
        parent_id = parent.id if parent is not None else None
        revision = ProjectRevision(
            id=stable_id("project-revision", project_id, parent_id or "root", tree_hash),
            project_id=project_id,
            run_id=run_id,
            tree_hash=tree_hash,
            sequence=(parent.sequence + 1) if parent is not None else 1,
            parent_id=parent_id,
            manifest=manifest,
            changes=changes,
        )
        self.store.upsert_node(_revision_node(revision), return_clone=False)
        if parent_id:
            self.store.upsert_edge(_revision_edge(revision.id, parent_id, "PARENT_REVISION"), return_clone=False)
        if self.store.get_node(run_id, clone=False) is not None:
            self.store.upsert_edge(_revision_edge(revision.id, run_id, "DERIVED_FROM"), return_clone=False)
        for change in changes:
            if self.store.get_node(change.artifact_id, clone=False) is None:
                continue
            edge_properties = change.to_dict()
            edge_properties["changed_artifact_id"] = edge_properties.pop("artifact_id")
            self.store.upsert_edge(
                _revision_edge(
                    revision.id,
                    change.artifact_id,
                    "CHANGES",
                    properties=edge_properties,
                ),
                return_clone=False,
            )
        return revision

    def latest(self, project_id: str) -> ProjectRevision | None:
        revisions = self.list(project_id=project_id, limit=1)
        return revisions[0] if revisions else None

    def list(self, *, project_id: str | None = None, limit: int = 20) -> list[ProjectRevision]:
        limit = max(0, limit)
        property_name = "project_id" if project_id is not None else "kind"
        property_value = project_id if project_id is not None else "project_revision"
        nodes = self.store.find_nodes_by_property(
            property_name,
            property_value,
            type_="ProjectRevision",
            limit=max(limit * 5, 100),
            clone=False,
        )
        revisions = [
            _revision_from_node(node)
            for node in nodes
            if node.properties.get("kind") == "project_revision"
        ]
        revisions.sort(key=lambda item: (item.sequence, item.created_at, item.id), reverse=True)
        return revisions[:limit]

    def get(self, revision_id: str) -> ProjectRevision | None:
        node = self.store.get_node(revision_id)
        if node is None or node.type != "ProjectRevision" or node.properties.get("kind") != "project_revision":
            return None
        return _revision_from_node(node)


def _manifest(artifacts: Iterable[SourceArtifact]) -> dict[str, dict[str, str]]:
    return {
        artifact.relative_path.replace("\\", "/"): {
            "artifact_id": artifact.id,
            "sha256": artifact.sha256,
        }
        for artifact in sorted(artifacts, key=lambda item: item.relative_path)
    }


def _tree_hash(manifest: dict[str, dict[str, str]]) -> str:
    parts: list[str] = []
    for path, entry in sorted(manifest.items()):
        parts.extend((path, entry["sha256"]))
    return stable_hash(parts, length=64)


def _changes(
    old: dict[str, dict[str, str]],
    new: dict[str, dict[str, str]],
) -> list[FileChange]:
    result: list[FileChange] = []
    for path in sorted(set(old) | set(new)):
        before = old.get(path)
        after = new.get(path)
        if before == after:
            continue
        status = "added" if before is None else "deleted" if after is None else "modified"
        entry = after or before or {}
        result.append(
            FileChange(
                path=path,
                status=status,
                artifact_id=str(entry.get("artifact_id") or ""),
                old_sha256=str(before["sha256"]) if before else None,
                new_sha256=str(after["sha256"]) if after else None,
            )
        )
    return result


def _revision_node(revision: ProjectRevision) -> MemoryNode:
    changed_paths = ", ".join(change.path for change in revision.changes[:20])
    properties = revision.to_dict(include_manifest=True)
    properties["kind"] = "project_revision"
    return MemoryNode(
        id=revision.id,
        type="ProjectRevision",
        label=f"Project revision {revision.sequence}",
        text=f"Revision {revision.sequence}; changed files: {changed_paths}",
        canonical_key=revision.id,
        properties=properties,
        salience=0.2,
        confidence=1.0,
        stability=1.0,
        volatility=0.0,
        created_at=revision.created_at,
        updated_at=revision.created_at,
    )


def _revision_edge(
    from_id: str,
    to_id: str,
    edge_type: str,
    *,
    properties: dict[str, Any] | None = None,
) -> MemoryEdge:
    return MemoryEdge(
        id=stable_id("revision-edge", from_id, edge_type, to_id),
        from_id=from_id,
        to_id=to_id,
        type=edge_type,
        properties=dict(properties or {}),
    )


def _revision_from_node(node: MemoryNode) -> ProjectRevision:
    data: dict[str, Any] = dict(node.properties)
    raw_manifest = data.get("manifest") or {}
    return ProjectRevision(
        id=str(data.get("id") or node.id),
        project_id=str(data["project_id"]),
        run_id=str(data["run_id"]),
        tree_hash=str(data["tree_hash"]),
        sequence=int(data.get("sequence") or 0),
        parent_id=str(data["parent_id"]) if data.get("parent_id") else None,
        manifest={str(path): {str(key): str(value) for key, value in dict(entry).items()} for path, entry in dict(raw_manifest).items()},
        changes=[FileChange(**dict(change)) for change in list(data.get("changes") or [])],
        created_at=str(data.get("created_at") or node.created_at),
    )
