"""Destructive storage maintenance implemented through verified clean rebuilds."""
from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import uuid

from ..artifacts.cache import artifact_cache_path
from ..artifacts.options import CompilationOptions
from ..config import REQLConfig
from ..domain.exceptions import StorageError
from .adapters.block_store import exclusive_store_lock


def clear_project_storage(
    storage_path: str | Path,
    project_path: str | Path,
    *,
    config: REQLConfig,
    config_path: str | Path | None = None,
    max_file_size_bytes: int,
    parsing_options: CompilationOptions,
) -> dict[str, Any]:
    """Replace a store with a clean graph compiled from the current project tree.

    The existing store remains untouched until the clean compilation succeeds.
    The project cache is rebuilt in the same maintenance window and restored if
    compilation or replacement fails.
    """
    project_root = Path(project_path).expanduser().resolve(strict=False)
    if not project_root.exists():
        raise StorageError(f"Project path does not exist: {project_root}")
    if not project_root.is_dir():
        raise StorageError(f"Storage clear requires a project directory: {project_root}")

    target = Path(storage_path).expanduser().resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    cache_path = artifact_cache_path(project_root)
    cache_backup = cache_path.with_name(f"{cache_path.name}.clear-backup-{uuid.uuid4().hex}")
    before_bytes = target.stat().st_size if target.exists() else 0
    committed = False

    with exclusive_store_lock(target):
        _backup_cache(cache_path, cache_backup)
        try:
            with TemporaryDirectory(prefix=f".{target.name}.clear-", dir=target.parent) as temporary_dir:
                staged_path = Path(temporary_dir) / target.name
                payload = _compile_clean_store(
                    staged_path,
                    project_root,
                    config=config,
                    config_path=config_path,
                    max_file_size_bytes=max_file_size_bytes,
                    parsing_options=parsing_options,
                )
                os.replace(staged_path, target)
                committed = True

            removed_sidecars = _remove_obsolete_sidecars(target)
        except Exception:
            if not committed:
                _restore_cache(cache_path, cache_backup)
            raise
        finally:
            if committed:
                cache_backup.unlink(missing_ok=True)

    after_bytes = target.stat().st_size
    return {
        "path": str(target),
        "project_path": str(project_root),
        "bytes_before": before_bytes,
        "bytes_after": after_bytes,
        "bytes_reclaimed": max(0, before_bytes - after_bytes),
        "cache_path": str(cache_path),
        "cache_rebuilt": bool(config.cache.enabled),
        "removed_sidecars": removed_sidecars,
        **payload,
    }


def _compile_clean_store(
    staged_path: Path,
    project_root: Path,
    *,
    config: REQLConfig,
    config_path: str | Path | None,
    max_file_size_bytes: int,
    parsing_options: CompilationOptions,
) -> dict[str, Any]:
    # Imported lazily to keep the storage package importable by MemoryGraph.
    from api.memory_graph import MemoryGraph

    graph = MemoryGraph.open(staged_path, config=config, defer_lexical_index=True)
    try:
        result = graph.compile_project(
            project_root,
            max_file_size_bytes=max_file_size_bytes,
            include_patterns=config.scan.include,
            exclude_patterns=config.scan.exclude,
            config_path=config_path,
            cache_enabled=config.cache.enabled,
            parsing_options=parsing_options,
        )
        if result.run.errors:
            rendered = "; ".join(result.run.errors[:5])
            if len(result.run.errors) > 5:
                rendered += f"; and {len(result.run.errors) - 5} more"
            raise StorageError(f"Clean project compilation failed; existing storage was preserved: {rendered}")

        graph.store.compact_storage()
        return {
            "files_seen": result.run.files_seen,
            "files_changed": result.run.files_changed,
            "nodes_after": graph.store.count_nodes(),
            "edges_after": graph.store.count_edges(),
            "archived_nodes_after": graph.store.count_nodes(statuses={"archived", "deleted"}),
            "compilation_run_id": result.run.id,
            "graph_delta_id": result.delta.id,
        }
    finally:
        graph.close()


def _backup_cache(cache_path: Path, backup_path: Path) -> None:
    backup_path.unlink(missing_ok=True)
    if cache_path.exists():
        os.replace(cache_path, backup_path)


def _restore_cache(cache_path: Path, backup_path: Path) -> None:
    cache_path.unlink(missing_ok=True)
    if backup_path.exists():
        os.replace(backup_path, cache_path)


def _remove_obsolete_sidecars(target: Path) -> list[str]:
    removed: list[str] = []
    for suffix in (".wal", ".usage.jsonl"):
        sidecar = target.with_name(f"{target.name}{suffix}")
        if suffix == ".usage.jsonl":
            with exclusive_store_lock(sidecar):
                if sidecar.exists():
                    sidecar.unlink()
                    removed.append(str(sidecar))
        elif sidecar.exists():
            sidecar.unlink()
            removed.append(str(sidecar))
    return removed


__all__ = ["clear_project_storage"]
