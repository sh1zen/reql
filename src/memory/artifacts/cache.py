"""Incremental artifact compilation cache stored in project-local metadata."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..domain.ids import stable_id
from ..domain.models import MemoryNode
from ..domain.timeutils import utcnow_iso
from ..storage.graph_store import GraphStore
from .fingerprint import DEFAULT_CHUNKING_VERSION, DEFAULT_PARSER_VERSION, options_hash
from .models import ScanResult, SourceArtifact

ARTIFACT_CACHE_FILENAME = "artifact-cache.json"
ARTIFACT_CACHE_FORMAT = "reql-artifact-cache-v1"


@dataclass(slots=True)
class ArtifactCacheEntry:
    id: str
    project_id: str
    artifact_id: str
    relative_path: str
    sha256: str
    size_bytes: int
    mtime: float
    parser_version: str
    chunking_version: str
    options_hash: str
    compiled_at: str
    status: str = "active"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class DirtySet:
    changed_artifact_ids: set[str] = field(default_factory=set)
    deleted_artifact_ids: set[str] = field(default_factory=set)
    affected_node_ids: set[str] = field(default_factory=set)
    affected_edge_ids: set[str] = field(default_factory=set)
    affected_community_ids: set[str] = field(default_factory=set)

    def to_dict(self) -> dict[str, object]:
        return {
            "changed_artifact_ids": sorted(self.changed_artifact_ids),
            "deleted_artifact_ids": sorted(self.deleted_artifact_ids),
            "affected_node_ids": sorted(self.affected_node_ids),
            "affected_edge_ids": sorted(self.affected_edge_ids),
            "affected_community_ids": sorted(self.affected_community_ids),
        }


class ArtifactCache:
    """Query and update project artifact cache metadata."""

    def __init__(
        self,
        store: GraphStore,
        *,
        cache_path: str | Path | None = None,
        parser_version: str = DEFAULT_PARSER_VERSION,
        chunking_version: str = DEFAULT_CHUNKING_VERSION,
        compile_options: dict[str, object] | None = None,
    ) -> None:
        self.store = store
        self.disk = DiskArtifactCache(cache_path) if cache_path is not None else None
        self.parser_version = parser_version
        self.chunking_version = chunking_version
        self.options_hash = options_hash(compile_options)

    def entry_id(self, project_id: str, artifact_id: str) -> str:
        return stable_id("artifact-cache", project_id, artifact_id)

    def get_entry(self, project_id: str, artifact_id: str) -> ArtifactCacheEntry | None:
        if self.disk is not None:
            entry = self.disk.get_entry(project_id, artifact_id)
            if entry is not None and entry.status == "active":
                return entry
        node = self.store.get_node_by_key("ArtifactCacheEntry", f"{project_id}:{artifact_id}")
        if node is None or node.status != "active":
            return None
        return _entry_from_node(node)

    def project_entries(self, project_id: str, *, active_only: bool = True) -> list[ArtifactCacheEntry]:
        entries_by_artifact: dict[str, ArtifactCacheEntry] = {}
        if self.disk is not None:
            for entry in self.disk.project_entries(project_id, active_only=active_only):
                entries_by_artifact[entry.artifact_id] = entry
        for node in self.store.find_nodes_by_property("project_id", project_id, type_="ArtifactCacheEntry", limit=100000):
            if active_only and node.status != "active":
                continue
            entry = _entry_from_node(node)
            entries_by_artifact.setdefault(entry.artifact_id, entry)
        return list(entries_by_artifact.values())

    def project_entry_map(self, project_id: str, *, active_only: bool = True) -> dict[str, ArtifactCacheEntry]:
        entries: dict[str, ArtifactCacheEntry] = {}
        for entry in self.project_entries(project_id, active_only=active_only):
            entries.setdefault(entry.artifact_id, entry)
        return entries

    def is_cached(self, artifact: SourceArtifact) -> bool:
        entry = self.get_entry(artifact.project_id, artifact.id)
        return entry is not None and self.entry_matches_artifact(entry, artifact)

    def entry_matches_artifact(self, entry: ArtifactCacheEntry, artifact: SourceArtifact) -> bool:
        return (
            entry.sha256 == artifact.sha256
            and entry.size_bytes == artifact.size_bytes
            and float(entry.mtime) == float(artifact.mtime)
            and entry.parser_version == self.parser_version
            and entry.chunking_version == self.chunking_version
            and entry.options_hash == self.options_hash
            and entry.status == "active"
        )

    def dirty_set(self, scan: ScanResult, entries: dict[str, ArtifactCacheEntry] | None = None) -> DirtySet:
        dirty = DirtySet()
        entry_map = entries if entries is not None else self.project_entry_map(scan.project.id, active_only=True)
        current_artifact_ids = {artifact.id for artifact in scan.artifacts}
        for artifact in scan.artifacts:
            entry = entry_map.get(artifact.id)
            if entry is None or not self.entry_matches_artifact(entry, artifact):
                dirty.changed_artifact_ids.add(artifact.id)
        for entry in entry_map.values():
            if entry.artifact_id not in current_artifact_ids:
                dirty.deleted_artifact_ids.add(entry.artifact_id)
        return dirty

    def recoverable_artifact_ids(
        self,
        scan: ScanResult,
        artifact_ids: set[str],
        entries: dict[str, ArtifactCacheEntry] | None = None,
    ) -> set[str]:
        recoverable: set[str] = set()
        entry_map = entries if entries is not None else self.project_entry_map(scan.project.id, active_only=True)
        for artifact in scan.artifacts:
            if artifact.id not in artifact_ids or artifact.id in entry_map:
                continue
            node = self.store.get_node(artifact.id)
            if node is None or node.status != "active":
                continue
            props = node.properties
            if (
                props.get("last_compiled_at")
                and str(props.get("project_id")) == artifact.project_id
                and str(props.get("relative_path")) == artifact.relative_path
                and str(props.get("sha256")) == artifact.sha256
                and int(props.get("size_bytes", -1)) == artifact.size_bytes
            ):
                recoverable.add(artifact.id)
        return recoverable

    def deleted_project_artifact_ids(self, scan: ScanResult) -> set[str]:
        current_artifact_ids = {artifact.id for artifact in scan.artifacts}
        deleted: set[str] = set()
        for node in self.store.find_nodes_by_property("project_id", scan.project.id, type_="SourceArtifact", limit=100000):
            if node.status != "active" or not node.properties.get("last_compiled_at"):
                continue
            if node.id not in current_artifact_ids:
                deleted.add(node.id)
        return deleted

    def upsert_entry(self, artifact: SourceArtifact, *, compiled_at: str | None = None) -> ArtifactCacheEntry:
        now = compiled_at or utcnow_iso()
        entry = ArtifactCacheEntry(
            id=self.entry_id(artifact.project_id, artifact.id),
            project_id=artifact.project_id,
            artifact_id=artifact.id,
            relative_path=artifact.relative_path,
            sha256=artifact.sha256,
            size_bytes=artifact.size_bytes,
            mtime=artifact.mtime,
            parser_version=self.parser_version,
            chunking_version=self.chunking_version,
            options_hash=self.options_hash,
            compiled_at=now,
            status="active",
        )
        self.store.upsert_node(_entry_node(entry))
        if self.disk is not None:
            self.disk.upsert_entry(entry)
        return entry

    def clear_project(self, project_id: str) -> int:
        count = 0
        for entry in self.project_entries(project_id, active_only=True):
            archived_graph = self.archive_entry(entry.id)
            archived_disk = self.disk.archive_entry(project_id, entry.artifact_id) if self.disk is not None else False
            if archived_graph or archived_disk:
                count += 1
        return count

    def archive_entry(self, entry_id: str) -> bool:
        node = self.store.get_node(entry_id)
        if node is None or node.status == "archived":
            return False
        properties = dict(node.properties)
        properties["status"] = "archived"
        properties["updated_at"] = utcnow_iso()
        self.store.update_node_fields(node.id, status="archived", properties=properties)
        if self.disk is not None:
            project_id = str(properties.get("project_id") or "")
            artifact_id = str(properties.get("artifact_id") or "")
            if project_id and artifact_id:
                self.disk.archive_entry(project_id, artifact_id)
        return True

    def status_for_scan(self, scan: ScanResult) -> dict[str, object]:
        dirty = self.dirty_set(scan)
        total = len(scan.artifacts)
        return {
            "project": scan.project.to_dict(),
            "total_artifacts": total,
            "cached_artifacts": total - len(dirty.changed_artifact_ids),
            "dirty_artifacts": len(dirty.changed_artifact_ids),
            "deleted_artifacts": len(dirty.deleted_artifact_ids),
            "dirty_set": dirty.to_dict(),
            "cache_path": str(self.disk.path) if self.disk is not None else None,
        }


class DiskArtifactCache:
    """Small JSON artifact cache stored under a project's ``.reql`` directory."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve(strict=False)

    def get_entry(self, project_id: str, artifact_id: str) -> ArtifactCacheEntry | None:
        raw = self._project_entries(project_id).get(artifact_id)
        if not isinstance(raw, dict):
            return None
        try:
            return _entry_from_mapping(raw)
        except (KeyError, TypeError, ValueError):
            return None

    def project_entries(self, project_id: str, *, active_only: bool = True) -> list[ArtifactCacheEntry]:
        entries: list[ArtifactCacheEntry] = []
        for raw in self._project_entries(project_id).values():
            if not isinstance(raw, dict):
                continue
            try:
                entry = _entry_from_mapping(raw)
            except (KeyError, TypeError, ValueError):
                continue
            if active_only and entry.status != "active":
                continue
            entries.append(entry)
        return entries

    def upsert_entry(self, entry: ArtifactCacheEntry) -> None:
        data = self._load()
        project = self._project(data, entry.project_id)
        entries = project.setdefault("entries", {})
        if isinstance(entries, dict):
            entries[entry.artifact_id] = entry.to_dict()
        data["updated_at"] = utcnow_iso()
        self._write(data)

    def archive_entry(self, project_id: str, artifact_id: str) -> bool:
        data = self._load()
        projects = data.get("projects")
        if not isinstance(projects, dict):
            return False
        project = projects.get(project_id)
        if not isinstance(project, dict):
            return False
        entries = project.get("entries")
        if not isinstance(entries, dict):
            return False
        raw = entries.get(artifact_id)
        if not isinstance(raw, dict) or raw.get("status") == "archived":
            return False
        raw["status"] = "archived"
        raw["updated_at"] = utcnow_iso()
        data["updated_at"] = utcnow_iso()
        self._write(data)
        return True

    def _project_entries(self, project_id: str) -> dict[str, Any]:
        data = self._load()
        projects = data.get("projects")
        if not isinstance(projects, dict):
            return {}
        project = projects.get(project_id)
        if not isinstance(project, dict):
            return {}
        entries = project.get("entries")
        return entries if isinstance(entries, dict) else {}

    def _project(self, data: dict[str, Any], project_id: str) -> dict[str, Any]:
        projects = data.setdefault("projects", {})
        if not isinstance(projects, dict):
            projects = {}
            data["projects"] = projects
        project = projects.setdefault(project_id, {"entries": {}})
        if not isinstance(project, dict):
            project = {"entries": {}}
            projects[project_id] = project
        if not isinstance(project.get("entries"), dict):
            project["entries"] = {}
        return project

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"format": ARTIFACT_CACHE_FORMAT, "projects": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"format": ARTIFACT_CACHE_FORMAT, "projects": {}}
        if not isinstance(data, dict) or data.get("format") != ARTIFACT_CACHE_FORMAT:
            return {"format": ARTIFACT_CACHE_FORMAT, "projects": {}}
        if not isinstance(data.get("projects"), dict):
            data["projects"] = {}
        return data

    def _write(self, data: dict[str, Any]) -> None:
        data["format"] = ARTIFACT_CACHE_FORMAT
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_name(f"{self.path.name}.tmp")
        tmp_path.write_text(json.dumps(data, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp_path.replace(self.path)


def artifact_cache_path(project_root: str | Path) -> Path:
    return Path(project_root).expanduser().resolve(strict=False) / ".reql" / ARTIFACT_CACHE_FILENAME


def _entry_node(entry: ArtifactCacheEntry) -> MemoryNode:
    return MemoryNode(
        id=entry.id,
        type="ArtifactCacheEntry",
        label=entry.relative_path,
        canonical_key=f"{entry.project_id}:{entry.artifact_id}",
        properties=entry.to_dict(),
        salience=0.05,
        confidence=1.0,
        status=entry.status,
    )


def _entry_from_node(node: MemoryNode) -> ArtifactCacheEntry:
    data = dict(node.properties)
    return _entry_from_mapping({**data, "id": data.get("id") or node.id, "status": data.get("status") or node.status})


def _entry_from_mapping(data: dict[str, Any]) -> ArtifactCacheEntry:
    return ArtifactCacheEntry(
        id=str(data["id"]),
        project_id=str(data["project_id"]),
        artifact_id=str(data["artifact_id"]),
        relative_path=str(data["relative_path"]),
        sha256=str(data["sha256"]),
        size_bytes=int(data["size_bytes"]),
        mtime=float(data["mtime"]),
        parser_version=str(data["parser_version"]),
        chunking_version=str(data["chunking_version"]),
        options_hash=str(data["options_hash"]),
        compiled_at=str(data["compiled_at"]),
        status=str(data.get("status") or "active"),
    )
