"""Capture bounded source spans from files that match a committed graph revision."""
from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Sequence

from ....artifacts.revision import ProjectRevision, RevisionRepository
from ....domain.models import MemoryNode
from ....storage.graph_store import GraphStore


MAX_SOURCE_FILES = 3
MAX_SOURCE_REGIONS = 6
MAX_SOURCE_BYTES = 4 * 1024 * 1024
MAX_SOURCE_LINES = 120
MAX_SOURCE_CHARS = 6000


def _read_bounded_source(path: Path) -> bytes | None:
    """Share one byte ceiling for verified inspection and ordinary source previews."""
    with path.open("rb") as stream:
        content = stream.read(MAX_SOURCE_BYTES + 1)
    return content if len(content) <= MAX_SOURCE_BYTES else None


def capture_source_excerpts(store: GraphStore, reads: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Read each selected file once, merge overlaps, and expose exact coverage or failure."""
    regions: list[dict[str, Any]] = []
    for item in reads:
        start, end = item.get("line_start"), item.get("line_end")
        if type(start) is not int or type(end) is not int or start < 1 or end < start:
            continue
        node = store.get_node(str(item.get("node_id") or ""), clone=False)
        if node is None:
            continue
        artifact_id = str(node.properties.get("artifact_id") or "")
        # Only merge overlapping spans. Do not include unrequested intervening source.
        overlapping = [row for row in regions if artifact_id and row["artifact_id"] == artifact_id
                       and row["line_start"] <= end and row["line_end"] >= start]
        if overlapping:
            first = overlapping[0]
            first["line_start"] = min(start, *(row["line_start"] for row in overlapping))
            first["line_end"] = max(end, *(row["line_end"] for row in overlapping))
            for row in overlapping[1:]:
                regions.remove(row)
        else:
            regions.append({"path": item.get("path"), "line_start": start, "line_end": end,
                            "node_id": node.id, "artifact_id": artifact_id, "type": node.type})

    snapshots: dict[str, dict[str, Any]] = {}
    revisions: dict[str, ProjectRevision | None] = {}
    result: list[dict[str, Any]] = []
    lines_left, chars_left = MAX_SOURCE_LINES, MAX_SOURCE_CHARS
    for region in regions[:MAX_SOURCE_REGIONS]:
        artifact_id = region["artifact_id"]
        if artifact_id not in snapshots:
            if len(snapshots) >= MAX_SOURCE_FILES:
                break
            artifact = store.get_node(artifact_id, clone=False) if artifact_id else None
            project_id = str(artifact.properties.get("project_id") or "") if artifact else ""
            if project_id not in revisions:
                revisions[project_id] = RevisionRepository(store).latest(project_id) if project_id else None
            snapshots[artifact_id] = _source_snapshot(store, artifact, revisions[project_id])
        snapshot = snapshots[artifact_id]
        row = {**region, "source": "disk", "source_status": snapshot["status"]}
        if snapshot["status"] != "verified":
            row["reason"] = snapshot["reason"]
            result.append(row)
            continue
        if lines_left <= 0 or chars_left <= 0:
            break
        lines = snapshot["lines"]
        start, end = region["line_start"], min(region["line_end"], len(lines))
        selected: list[str] = []
        used_chars = 0
        for line in lines[start - 1:end][:lines_left]:
            cost = len(line) + bool(selected)
            if used_chars + cost > chars_left:
                break
            selected.append(line)
            used_chars += cost
        if not selected:
            row.update(source_status="unavailable", reason="source span is outside the file or exceeds the remaining budget")
        else:
            actual_end = start + len(selected) - 1
            row.update(line_end=actual_end, requested_line_end=region["line_end"],
                       text="\n".join(selected), sha256=snapshot["sha256"],
                       truncated=actual_end < region["line_end"])
            if row["truncated"]:
                row["next_line"] = actual_end + 1
            lines_left -= len(selected)
            chars_left -= used_chars
        result.append(row)
    return result


def _source_snapshot(store: GraphStore, artifact: MemoryNode | None, revision: ProjectRevision | None) -> dict[str, Any]:
    """Check project confinement and committed fingerprints before exposing file text."""
    unavailable = {"status": "unavailable", "reason": "no committed source metadata for this span"}
    if artifact is None or artifact.type != "SourceArtifact" or artifact.status != "active" or revision is None:
        return unavailable
    props = artifact.properties
    relative = str(props.get("relative_path") or "").replace("\\", "/")
    entry = revision.manifest.get(relative)
    compiled_at = props.get("last_compiled_at")
    if (not entry or entry.get("artifact_id") != artifact.id or not compiled_at
            or not props.get("compiled_sha256") or props["compiled_sha256"] != props.get("sha256")
            or entry.get("sha256") != props.get("sha256")):
        return {"status": "stale", "reason": "source metadata does not match the committed revision; refresh the graph"}
    project = store.get_node(str(props.get("project_id") or ""), clone=False)
    root_path = project.properties.get("root_path") if project is not None and project.type == "Project" else None
    if not isinstance(root_path, str) or not root_path or not Path(root_path).is_absolute():
        return unavailable
    if not relative or PureWindowsPath(relative).drive or PurePosixPath(relative).is_absolute() or ".." in PurePosixPath(relative).parts:
        return {"status": "unavailable", "reason": "source path is outside the project"}
    try:
        root = Path(root_path).resolve()
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            return {"status": "unavailable", "reason": "source path is outside the project"}
        if not path.is_file():
            return {"status": "stale", "reason": "indexed source file is missing or is no longer a regular file"}
        content = _read_bounded_source(path)
        if content is None:
            return {"status": "unavailable", "reason": "source file exceeds the 4 MiB read limit"}
        digest = hashlib.sha256(content).hexdigest()
        if digest != entry["sha256"]:
            return {"status": "stale", "reason": "source changed after indexing; graph spans require a refresh"}
        return {"status": "verified", "sha256": digest, "lines": content.decode("utf-8-sig").splitlines()}
    except (OSError, UnicodeError, ValueError) as exc:
        return {"status": "unavailable", "reason": f"cannot read source: {type(exc).__name__}"}
