"""Incremental project compilation orchestration."""
from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..diagnostics import PerformanceLogger
from ..artifacts.cache import ArtifactCache, DirtySet, artifact_cache_path
from ..artifacts.compiler import ArtifactCompilationResult, ArtifactCompiler, archive_artifact_fragments, link_document_fragments_to_code
from ..artifacts.context_scope import artifact_context_scope
from ..artifacts.delta import CompilationRun, DeltaRepository, GraphDelta
from ..artifacts.fingerprint import DEFAULT_CHUNKING_VERSION, DEFAULT_PARSER_VERSION, normalize_path, project_id
from ..artifacts.models import ScanResult, SourceArtifact
from ..artifacts.project import ProjectRegistry
from ..artifacts.scanner import DEFAULT_MAX_FILE_SIZE_BYTES, ProjectScanner
from ..domain.ids import stable_id
from ..domain.models import MemoryEdge, MemoryNode
from ..domain.timeutils import utcnow_iso
from ..storage.graph_store import GraphStore

ORPHAN_DIRECTORY_MIN_FILES = 2
ORPHAN_DIRECTORY_FINDING_TYPE = "possibly_orphan_directory"
ORPHAN_DIRECTORY_EDGE_TYPES = {
    "IMPORTS",
    "IMPORTS_FROM",
    "DEPENDS_ON",
    "CALLS",
    "INSTANTIATES",
    "REFERENCES",
    "READS",
    "TESTS",
}
ORPHAN_DIRECTORY_COMMON_ROOTS = {"src", "tests", "test", "docs"}
ORPHAN_DIRECTORY_NAME_HINTS = {"legacy", "old", "unused", "obsolete", "dead", "archive", "archived", "backup", "backups", "deprecated"}
UNUSED_SYMBOL_FINDING_TYPES = {"possibly_unused_function", "possibly_unused_method", "possibly_unused_class"}
UNUSED_SYMBOL_USAGE_EDGE_TYPES = {
    "CALLS",
    "USES",
    "REFERENCES",
    "READS",
    "RETURNS",
    "RAISES",
    "INHERITS",
    "IMPLEMENTS",
    "INSTANTIATES",
    "IMPORTS",
    "RE_EXPORTS",
    "TESTS",
}
UNUSED_SYMBOL_NODE_TYPES = {"Function", "Class", "Interface", "Method"}


@dataclass(slots=True)
class CompileProjectResult:
    scan: ScanResult
    dirty_set: DirtySet
    run: CompilationRun
    delta: GraphDelta

    def to_dict(self) -> dict[str, object]:
        return {
            "scan": self.scan.to_dict(),
            "dirty_set": self.dirty_set.to_dict(),
            "run": self.run.to_dict(),
            "delta": self.delta.to_dict(),
        }


class IncrementalCompilationService:
    """Scans projects, compiles dirty artifacts, updates cache, and records deltas."""

    def __init__(
        self,
        store: GraphStore,
        *,
        compiler: ArtifactCompiler | None = None,
        parser_version: str = DEFAULT_PARSER_VERSION,
        chunking_version: str = DEFAULT_CHUNKING_VERSION,
        compile_options: dict[str, object] | None = None,
        profile_logger: PerformanceLogger | None = None,
    ) -> None:
        self.store = store
        self.profile_logger = profile_logger
        self.project_registry = ProjectRegistry(store)
        self.compiler = compiler or ArtifactCompiler()
        self.cache = ArtifactCache(
            store,
            parser_version=parser_version,
            chunking_version=chunking_version,
            compile_options=compile_options,
        )
        self.deltas = DeltaRepository(store)

    def compile_path(
        self,
        path: str | Path,
        *,

        max_file_size_bytes: int = DEFAULT_MAX_FILE_SIZE_BYTES,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        cache_enabled: bool = True,
        parsing_options: dict[str, object] | None = None,
    ) -> CompileProjectResult:
        profile = self.profile_logger
        compile_start = profile.span("compile.total", path=Path(path).expanduser().resolve(strict=False)) if profile else nullcontext()
        with compile_start:
            return self._compile_path(
                path,

                max_file_size_bytes=max_file_size_bytes,
                include_patterns=include_patterns,
                exclude_patterns=exclude_patterns,
                cache_enabled=cache_enabled,
                parsing_options=parsing_options,
            )

    def _compile_path(
        self,
        path: str | Path,
        *,

        max_file_size_bytes: int,
        include_patterns: list[str] | None,
        exclude_patterns: list[str] | None,
        cache_enabled: bool,
        parsing_options: dict[str, object] | None,
    ) -> CompileProjectResult:
        profile = self.profile_logger
        scanner = ProjectScanner(
            max_file_size_bytes=max_file_size_bytes,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
        )
        with (profile.span("compile.scan", path=Path(path).expanduser().resolve(strict=False)) if profile else nullcontext()):
            scan = scanner.scan(path)
        if profile:
            profile.event("compile.scan.result", category="counter", artifacts=len(scan.artifacts), skipped=len(scan.skipped_files), errors=len(scan.errors))
        with (profile.span("compile.plan", artifacts=len(scan.artifacts), cache_enabled=cache_enabled) if profile else nullcontext()):
            compile_options = self._compile_options(parsing_options)
            cache = self._cache_for_options(compile_options, project_root=scan.project.root_path)
            compiler = self._compiler_for_options(parsing_options)
            cache_entries = cache.project_entry_map(scan.project.id, active_only=True) if cache_enabled else {}
            if cache_enabled:
                dirty = cache.dirty_set(scan, entries=cache_entries)
            else:
                dirty = DirtySet(
                    changed_artifact_ids={artifact.id for artifact in scan.artifacts},
                    deleted_artifact_ids=set(),
                )
            recovered_artifact_ids = cache.recoverable_artifact_ids(scan, dirty.changed_artifact_ids, entries=cache_entries) if cache_enabled else set()
            dirty.changed_artifact_ids.difference_update(recovered_artifact_ids)
            if cache_enabled:
                dirty.deleted_artifact_ids.update(cache.deleted_project_artifact_ids(scan))
        run = self.deltas.new_run(project_id=scan.project.id)
        run.files_seen = len(scan.artifacts)
        run.files_changed = len(dirty.changed_artifact_ids)
        run.files_skipped = len(scan.artifacts) - run.files_changed
        run.files_deleted = len(dirty.deleted_artifact_ids)

        artifacts_by_id = {artifact.id: artifact for artifact in scan.artifacts}
        aggregate = _Aggregate()
        if profile:
            profile.event(
                "compile.plan.result",
                category="counter",
                files_seen=run.files_seen,
                files_changed=run.files_changed,
                files_skipped=run.files_skipped,
                files_deleted=run.files_deleted,
            )

        with (profile.span("compile.transaction", changed=run.files_changed, deleted=run.files_deleted) if profile else nullcontext()):
            with self.store.transaction():
                delta = self._apply_compile_transaction(scan, dirty, compiler, cache, cache_enabled, recovered_artifact_ids, artifacts_by_id, aggregate, run, profile)
        checkpoint = self._checkpoint_store_if_needed(profile)

        for node_id in aggregate.affected_node_ids:
            dirty.affected_node_ids.add(node_id)
        if profile:
            profile.event(
                "compile.result",
                category="counter",
                status=run.status,
                nodes_created=run.nodes_created,
                nodes_updated=run.nodes_updated,
                edges_created=run.edges_created,
                edges_updated=run.edges_updated,
                errors=len(run.errors),
                checkpointed=bool(checkpoint.get("checkpointed")) if checkpoint else False,
            )
        return CompileProjectResult(scan=scan, dirty_set=dirty, run=run, delta=delta)

    def _checkpoint_store_if_needed(self, profile: PerformanceLogger | None) -> dict[str, Any]:
        checkpoint = getattr(self.store, "checkpoint_if_needed", None)
        if checkpoint is None:
            return {}
        if profile:
            with profile.span("compile.checkpoint"):
                result = checkpoint()
            profile.event("compile.checkpoint.result", category="counter", **result)
            return dict(result)
        return dict(checkpoint())

    def _apply_compile_transaction(
        self,
        scan: ScanResult,
        dirty: DirtySet,
        compiler: ArtifactCompiler,
        cache: ArtifactCache,
        cache_enabled: bool,
        recovered_artifact_ids: set[str],
        artifacts_by_id: dict[str, SourceArtifact],
        aggregate: "_Aggregate",
        run: CompilationRun,
        profile: PerformanceLogger | None,
    ) -> GraphDelta:
        for artifact_id in sorted(recovered_artifact_ids):
            artifact = artifacts_by_id[artifact_id]
            with (profile.span("compile.cache_recover", artifact_id=artifact.id, relative_path=artifact.relative_path) if profile else nullcontext()):
                cache.upsert_entry(artifact, compiled_at=utcnow_iso())
        if dirty.changed_artifact_ids:
            with (profile.span("compile.register_delta", changed=len(dirty.changed_artifact_ids)) if profile else nullcontext()):
                scan.registration = self.project_registry.register_scan_delta(scan, dirty.changed_artifact_ids)
        for artifact_id in sorted(dirty.changed_artifact_ids):
            artifact = artifacts_by_id[artifact_id]
            try:
                with (profile.span("compile.artifact", artifact_id=artifact.id, relative_path=artifact.relative_path, size_bytes=artifact.size_bytes, artifact_type=artifact.artifact_type, language=artifact.language or "") if profile else nullcontext()):
                    result = compiler.compile_artifact(self.store, artifact)
            except Exception as exc:  # keep prior graph/cache intact for this artifact
                run.errors.append(f"{artifact.relative_path}: {exc}")
                if profile:
                    profile.event("compile.artifact.error", category="error", artifact_id=artifact.id, relative_path=artifact.relative_path, error=str(exc))
                continue
            for error in result.errors:
                run.errors.append(f"{artifact.relative_path}: {error}")
            if cache_enabled:
                with (profile.span("compile.cache_upsert", artifact_id=artifact.id, relative_path=artifact.relative_path) if profile else nullcontext()):
                    cache.upsert_entry(artifact, compiled_at=utcnow_iso())
            aggregate.add(result)
            if profile:
                profile.event(
                    "compile.artifact.result",
                    category="counter",
                    artifact_id=artifact.id,
                    relative_path=artifact.relative_path,
                    added_nodes=len(result.added_nodes),
                    updated_nodes=len(result.updated_nodes),
                    added_edges=len(result.added_edges),
                    updated_edges=len(result.updated_edges),
                    archived_nodes=len(result.archived_nodes),
                    archived_edges=len(result.archived_edges),
                    errors=len(result.errors),
                )

        if dirty.changed_artifact_ids and self._document_ingest_enabled(compiler):
            link_artifact_ids = self._document_code_link_scope(dirty.changed_artifact_ids, artifacts_by_id, compiler)
            with (profile.span("compile.link_documents_to_code", changed=len(dirty.changed_artifact_ids), scoped=link_artifact_ids is not None) if profile else nullcontext()):
                link_result = link_document_fragments_to_code(
                    self.store,

                    project_id=scan.project.id,
                    artifact_ids=link_artifact_ids,
                )
            aggregate.add(link_result)
            if profile:
                profile.event(
                    "compile.link_documents_to_code.result",
                    category="counter",
                    added_edges=len(link_result.added_edges),
                    updated_edges=len(link_result.updated_edges),
                    archived_edges=len(link_result.archived_edges),
                )

        if dirty.changed_artifact_ids and not run.errors:
            with (profile.span("compile.refresh_unused_symbol_findings", changed=len(dirty.changed_artifact_ids)) if profile else nullcontext()):
                aggregate.add(self._refresh_unused_symbol_findings(scan))

        for artifact_id in sorted(dirty.deleted_artifact_ids):
            node = self.store.get_node(artifact_id, clone=False)
            if node is None:
                continue
            with (profile.span("compile.delete_archive", artifact_id=artifact_id) if profile else nullcontext()):
                archive_result = archive_artifact_fragments(self.store, node)
                self._archive_deleted_artifact_node(node.id)
                file_archive_result = self._archive_file_nodes(run.project_id, artifact_id)
            aggregate.add(archive_result)
            aggregate.add(file_archive_result)
            aggregate.archived_nodes.add(artifact_id)
            aggregate.affected_node_ids.add(artifact_id)
            cache_entry = cache.get_entry(scan.project.id, artifact_id)
            if cache_enabled and cache_entry is not None:
                cache.archive_entry(cache_entry.id)

        if not run.errors and (dirty.changed_artifact_ids or dirty.deleted_artifact_ids):
            with (profile.span("compile.orphan_directories", artifacts=len(scan.artifacts)) if profile else nullcontext()):
                aggregate.add(self._refresh_orphan_directory_findings(scan))

        run.completed_at = utcnow_iso()
        run.status = "failed" if run.errors else "completed"
        run.nodes_created = len(aggregate.added_nodes)
        run.nodes_updated = len(aggregate.updated_nodes)
        run.edges_created = len(aggregate.added_edges)
        run.edges_updated = len(aggregate.updated_edges)

        delta = GraphDelta(
            id=self._new_delta_id(),
            run_id=run.id,

            project_id=scan.project.id,
            artifact_id="*",
            added_nodes=sorted(aggregate.added_nodes),
            updated_nodes=sorted(aggregate.updated_nodes),
            archived_nodes=sorted(aggregate.archived_nodes),
            added_edges=sorted(aggregate.added_edges),
            updated_edges=sorted(aggregate.updated_edges),
            archived_edges=sorted(aggregate.archived_edges),
            affected_node_ids=sorted(aggregate.affected_node_ids),
            affected_community_ids=sorted(aggregate.affected_community_ids),
        )
        with (profile.span("compile.persist_delta", run_id=run.id, delta_id=delta.id) if profile else nullcontext()):
            self.deltas.persist_run(run)
            self.deltas.persist_delta(delta)
        return delta

    def _archive_deleted_artifact_node(self, node_id: str) -> None:
        node = self.store.get_node(node_id, clone=False)
        if node is None or node.status == "archived":
            return
        properties = dict(node.properties)
        properties["status"] = "archived"
        properties["updated_at"] = utcnow_iso()
        self.store.update_node_fields(node.id, status="archived", properties=properties)

    def _archive_file_nodes(self, project_id_: str, artifact_id: str) -> ArtifactCompilationResult:
        result = ArtifactCompilationResult(artifact_id=artifact_id)
        for node in self.store.find_nodes_by_property("artifact_id", artifact_id, type_="File", limit=1000, clone=False):
            if node.properties.get("project_id") != project_id_ or node.status == "archived":
                continue
            properties = dict(node.properties)
            properties["status"] = "archived"
            properties["updated_at"] = utcnow_iso()
            self.store.update_node_fields(node.id, status="archived", properties=properties)
            result.archived_nodes.append(node.id)
            result.affected_node_ids.add(node.id)
            for edge in self.store.incident_edges([node.id], limit=10000, clone=False):
                if edge.properties.get("artifact_id") != artifact_id or edge.properties.get("status") == "archived":
                    continue
                edge_properties = dict(edge.properties)
                edge_properties["status"] = "archived"
                self.store.update_edge_fields(edge.id, properties=edge_properties)
                result.archived_edges.append(edge.id)
                result.affected_edge_ids.add(edge.id)
        return result

    def _refresh_orphan_directory_findings(self, scan: ScanResult) -> ArtifactCompilationResult:
        result = ArtifactCompilationResult(artifact_id="*")
        code_artifacts = [
            artifact
            for artifact in scan.artifacts
            if artifact.artifact_type == "code" and artifact_context_scope(artifact) == "code"
        ]
        grouped = _candidate_orphan_directory_groups(code_artifacts)
        if not grouped:
            return self._archive_stale_orphan_directory_findings(scan.project.id, set())

        project_nodes = self.store.find_nodes_by_property("project_id", scan.project.id, limit=100000, clone=False)
        path_by_node_id = {
            node.id: path
            for node in project_nodes
            if node.status == "active"
            if (path := _node_relative_path(node))
        }
        project_dependency_edges = [
            edge
            for edge in self.store.find_edges_by_property("project_id", scan.project.id, limit=100000, clone=False)
            if edge.type in ORPHAN_DIRECTORY_EDGE_TYPES and edge.properties.get("status") != "archived"
        ]
        current_finding_ids: set[str] = set()
        for directory, artifacts in grouped:
            if self._directory_has_external_inbound(directory, path_by_node_id, project_dependency_edges):
                continue
            directory_node = self.store.get_node(stable_id("directory", scan.project.id, directory), clone=False)
            if directory_node is None or directory_node.status == "archived":
                continue
            finding = _orphan_directory_finding_node(scan.project.id, directory_node, artifacts)
            stored, created = self.store.upsert_node(finding, return_clone=False)
            current_finding_ids.add(stored.id)
            if created:
                result.added_nodes.append(stored.id)
            else:
                result.updated_nodes.append(stored.id)
            result.affected_node_ids.add(stored.id)
            edge = _orphan_directory_finding_edge(scan.project.id, directory_node, stored)
            stored_edge, edge_created = self.store.upsert_edge(edge, return_clone=False)
            if edge_created:
                result.added_edges.append(stored_edge.id)
            else:
                result.updated_edges.append(stored_edge.id)
            result.affected_edge_ids.add(stored_edge.id)
        archive_result = self._archive_stale_orphan_directory_findings(scan.project.id, current_finding_ids)
        result.archived_nodes.extend(archive_result.archived_nodes)
        result.affected_node_ids.update(archive_result.affected_node_ids)
        return result

    def _directory_has_external_inbound(
        self,
        directory: str,
        path_by_node_id: dict[str, str],
        project_dependency_edges: list[MemoryEdge],
    ) -> bool:
        for edge in project_dependency_edges:
            target_path = path_by_node_id.get(edge.to_id)
            target_inside_directory = target_path is not None and _path_is_inside_directory(target_path, directory)
            if not target_inside_directory and not _edge_targets_directory_by_module(edge, directory):
                continue
            source_path = _normalize_relative_path(str(edge.properties.get("source_file") or "")) or path_by_node_id.get(edge.from_id)
            if not source_path or not _path_is_inside_directory(source_path, directory):
                return True
        return False

    def _archive_stale_orphan_directory_findings(self, project_id_: str, current_finding_ids: set[str]) -> ArtifactCompilationResult:
        result = ArtifactCompilationResult(artifact_id="*")
        for node in self.store.find_nodes_by_property("project_id", project_id_, type_="StaticAnalysisFinding", status="active", limit=100000, clone=False):
            if node.properties.get("finding_type") != ORPHAN_DIRECTORY_FINDING_TYPE or node.id in current_finding_ids:
                continue
            properties = dict(node.properties)
            properties["status"] = "archived"
            properties["updated_at"] = utcnow_iso()
            self.store.update_node_fields(node.id, status="archived", properties=properties)
            result.archived_nodes.append(node.id)
            result.affected_node_ids.add(node.id)
        return result

    def _refresh_unused_symbol_findings(self, scan: ScanResult) -> ArtifactCompilationResult:
        result = ArtifactCompilationResult(artifact_id="*")
        project_nodes = self.store.find_nodes_by_property("project_id", scan.project.id, status="active", limit=100000, clone=False)
        files_by_path = {
            path: node
            for node in project_nodes
            if node.type == "File"
            if (path := _node_relative_path(node))
        }
        symbols = [node for node in project_nodes if node.type in UNUSED_SYMBOL_NODE_TYPES]
        symbol_ids = {node.id for node in symbols}
        used_symbol_ids = self._used_project_symbol_ids(scan.project.id, symbol_ids)
        used_symbol_ids.update(self._imported_project_symbol_ids(scan.project.id, files_by_path, symbols, project_nodes, result))
        used_symbol_ids.update(self._unresolved_method_call_symbol_ids(project_nodes, symbols))

        if not used_symbol_ids:
            return result

        for finding in self.store.find_nodes_by_property("project_id", scan.project.id, type_="StaticAnalysisFinding", status="active", limit=100000, clone=False):
            if finding.properties.get("finding_type") not in UNUSED_SYMBOL_FINDING_TYPES:
                continue
            symbol_id = str(finding.properties.get("symbol_id") or "")
            if symbol_id not in used_symbol_ids:
                continue
            self._archive_finding_node(finding, result)
        return result

    def _used_project_symbol_ids(self, project_id_: str, symbol_ids: set[str]) -> set[str]:
        used: set[str] = set()
        if not symbol_ids:
            return used
        for edge in self.store.find_edges_by_property("project_id", project_id_, limit=100000, clone=False):
            if edge.type not in UNUSED_SYMBOL_USAGE_EDGE_TYPES:
                continue
            if edge.properties.get("status") == "archived":
                continue
            if edge.to_id in symbol_ids and edge.from_id != edge.to_id:
                used.add(edge.to_id)
        return used

    def _imported_project_symbol_ids(
        self,
        project_id_: str,
        files_by_path: dict[str, MemoryNode],
        symbols: list[MemoryNode],
        project_nodes: list[MemoryNode],
        result: ArtifactCompilationResult,
    ) -> set[str]:
        symbols_by_path_name: dict[tuple[str, str], list[MemoryNode]] = {}
        for symbol in symbols:
            relative_path = _node_relative_path(symbol)
            name = str(symbol.properties.get("name") or "")
            if relative_path and name:
                symbols_by_path_name.setdefault((relative_path, name), []).append(symbol)

        used: set[str] = set()
        for node in project_nodes:
            if node.type != "Import":
                continue
            import_name = str(node.properties.get("import_name") or node.properties.get("name") or "")
            if not import_name or import_name == "*":
                continue
            resolved_path = str(node.properties.get("resolved_relative_path") or "")
            if not resolved_path:
                resolved_path = _resolve_import_relative_path_from_index(files_by_path, _node_relative_path(node), str(node.properties.get("module") or ""), int(node.properties.get("level") or 0))
                if resolved_path:
                    properties = dict(node.properties)
                    properties["resolved_relative_path"] = resolved_path
                    properties["updated_at"] = utcnow_iso()
                    self.store.update_node_fields(node.id, properties=properties)
                    result.updated_nodes.append(node.id)
                    result.affected_node_ids.add(node.id)
            if not resolved_path:
                continue
            for symbol in symbols_by_path_name.get((resolved_path, import_name), []):
                if symbol.properties.get("project_id") == project_id_:
                    used.add(symbol.id)
        return used

    def _unresolved_method_call_symbol_ids(self, project_nodes: list[MemoryNode], symbols: list[MemoryNode]) -> set[str]:
        called_method_names: set[str] = set()
        for node in project_nodes:
            calls = node.properties.get("unresolved_calls")
            if not isinstance(calls, list):
                continue
            for call in calls:
                if not isinstance(call, dict):
                    continue
                target = str(call.get("target") or "")
                if "." not in target:
                    continue
                tail = target.rsplit(".", 1)[-1]
                if tail:
                    called_method_names.add(tail)
        if not called_method_names:
            return set()
        return {
            symbol.id
            for symbol in symbols
            if symbol.type == "Method" and str(symbol.properties.get("name") or "") in called_method_names
        }

    def _archive_finding_node(self, finding: MemoryNode, result: ArtifactCompilationResult) -> None:
        properties = dict(finding.properties)
        properties["status"] = "archived"
        properties["updated_at"] = utcnow_iso()
        properties["archived_reason"] = "project_wide_usage_detected"
        self.store.update_node_fields(finding.id, status="archived", properties=properties)
        result.archived_nodes.append(finding.id)
        result.affected_node_ids.add(finding.id)
        for edge in self.store.incident_edges([finding.id], edge_types={"HAS_FINDING"}, limit=10000):
            if edge.properties.get("status") == "archived":
                continue
            edge_properties = dict(edge.properties)
            edge_properties["status"] = "archived"
            edge_properties["updated_at"] = utcnow_iso()
            self.store.update_edge_fields(edge.id, properties=edge_properties)
            result.archived_edges.append(edge.id)
            result.affected_edge_ids.add(edge.id)

    def _document_code_link_scope(
        self,
        changed_artifact_ids: set[str],
        artifacts_by_id: dict[str, SourceArtifact],
        compiler: ArtifactCompiler,
    ) -> set[str] | None:
        changed_artifacts = [artifacts_by_id[artifact_id] for artifact_id in changed_artifact_ids if artifact_id in artifacts_by_id]
        if any(artifact.artifact_type == "code" for artifact in changed_artifacts):
            return None
        document_ids = {
            artifact.id
            for artifact in changed_artifacts
            if artifact.artifact_type in {"markdown", "text", "config", "data", "unknown"} and compiler.document_ingest_enabled(artifact)
        }
        return document_ids

    @staticmethod
    def _document_ingest_enabled(compiler: ArtifactCompiler) -> bool:
        return bool(getattr(compiler, "ingest_documents", True))

    def cache_status(
        self,
        path: str | Path,
        *,

        max_file_size_bytes: int = DEFAULT_MAX_FILE_SIZE_BYTES,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        cache_enabled: bool = True,
        parsing_options: dict[str, object] | None = None,
    ) -> dict[str, object]:
        scanner = ProjectScanner(
            max_file_size_bytes=max_file_size_bytes,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
        )
        scan = scanner.scan(path)
        if not cache_enabled:
            return {
                "cache_enabled": False,
                "project": scan.project.to_dict(),
                "total_artifacts": len(scan.artifacts),
                "cached_artifacts": 0,
                "dirty_artifacts": len(scan.artifacts),
                "deleted_artifacts": 0,
                "entries": [],
                "dirty": [artifact.id for artifact in scan.artifacts],
                "deleted": [],
            }
        return self._cache_for_options(self._compile_options(parsing_options), project_root=scan.project.root_path).status_for_scan(scan)

    def clear_cache(self, path: str | Path) -> dict[str, object]:
        root = normalize_path(path)
        project = self.store.get_node_by_key("Project", root)
        project_id_ = project.id if project else project_id(root)
        cache = self._cache_for_options(None, project_root=root)
        cleared = cache.clear_project(project_id_)
        return {"project_id": project_id_, "cleared_entries": cleared, "cache_path": str(artifact_cache_path(root))}

    def list_deltas(self, *, limit: int = 20) -> list[GraphDelta]:
        return self.deltas.list_deltas(limit=limit)

    def show_delta(self, delta_id: str) -> GraphDelta | None:
        return self.deltas.get_delta(delta_id)

    def _new_delta_id(self) -> str:
        from ..domain.ids import new_id

        return new_id("delta")

    def _cache_for_options(self, compile_options: dict[str, object] | None, *, project_root: str | Path | None = None) -> ArtifactCache:
        cache_path = artifact_cache_path(project_root) if project_root is not None else None
        return ArtifactCache(
            self.store,
            cache_path=cache_path,
            parser_version=DEFAULT_PARSER_VERSION,
            chunking_version=DEFAULT_CHUNKING_VERSION,
            compile_options=compile_options,
        )

    def _compiler_for_options(
        self,
        parsing_options: dict[str, object] | None,
    ) -> ArtifactCompiler:
        compile_settings = self._split_compile_options(parsing_options)
        if not compile_settings:
            return self.compiler
        document_policies = _document_policies(compile_settings)
        return ArtifactCompiler(
            enable_pdf=_document_format_ingest_enabled(document_policies, "pdf"),
            ingest_documents=bool(compile_settings.get("ingest_documents", True)),
            document_policies=document_policies,
        )

    @staticmethod
    def _compile_options(
        parsing_options: dict[str, object] | None,
    ) -> dict[str, object] | None:
        compile_settings = IncrementalCompilationService._split_compile_options(parsing_options)
        if not compile_settings:
            return None
        return {"compile": compile_settings}

    @staticmethod
    def _split_compile_options(options: dict[str, object] | None) -> dict[str, object]:
        raw = dict(options or {})
        compile_settings = raw.get("compile")
        if isinstance(compile_settings, dict):
            return dict(compile_settings)
        return {}


def _document_format_ingest_enabled(document_policies: list[dict[str, object]], format_name: str) -> bool:
    wanted = format_name.casefold()
    return any(
        str(item.get("format") or "").casefold() == wanted and _document_policy_ingest_enabled(item)
        for item in document_policies
    )


def _document_policies(compile_settings: dict[str, object]) -> list[dict[str, object]]:
    documents = compile_settings.get("documents", [])
    if not isinstance(documents, list):
        return []
    return [dict(item) for item in documents if isinstance(item, dict)]


def _document_policy_ingest_enabled(policy: dict[str, object]) -> bool:
    return bool(policy.get("ingest", True))


def _candidate_orphan_directory_groups(artifacts: list[SourceArtifact]) -> list[tuple[str, list[SourceArtifact]]]:
    by_directory: dict[str, dict[str, SourceArtifact]] = {}
    for artifact in artifacts:
        relative_path = _normalize_relative_path(artifact.relative_path)
        parent = Path(relative_path).parent.as_posix()
        if parent in {"", "."}:
            continue
        parts = [part for part in parent.split("/") if part]
        for index in range(1, len(parts) + 1):
            directory = "/".join(parts[:index])
            if _skip_orphan_directory_candidate(directory):
                continue
            by_directory.setdefault(directory, {})[artifact.id] = artifact

    raw_candidates = [
        (directory, sorted(items.values(), key=lambda item: item.relative_path))
        for directory, items in by_directory.items()
        if len(items) >= ORPHAN_DIRECTORY_MIN_FILES
    ]
    raw_candidates.sort(key=lambda item: (len(item[0].split("/")), item[0]))
    selected: list[tuple[str, list[SourceArtifact]]] = []
    selected_file_sets: list[set[str]] = []
    for directory, items in raw_candidates:
        file_set = {artifact.id for artifact in items}
        if any(existing.issuperset(file_set) for existing in selected_file_sets):
            continue
        selected.append((directory, items))
        selected_file_sets.append(file_set)
    return selected


def _skip_orphan_directory_candidate(directory: str) -> bool:
    parts = [part for part in directory.split("/") if part]
    if not parts:
        return True
    if len(parts) == 1 and parts[0] in ORPHAN_DIRECTORY_COMMON_ROOTS:
        return True
    return False


def _orphan_directory_confidence(directory: str) -> float:
    names = {part.casefold() for part in directory.split("/") if part}
    return 0.78 if names & ORPHAN_DIRECTORY_NAME_HINTS else 0.62


def _orphan_directory_cleanup_priority(directory: str) -> str:
    names = {part.casefold() for part in directory.split("/") if part}
    return "medium" if names & ORPHAN_DIRECTORY_NAME_HINTS else "low"


def _orphan_directory_finding_node(project_id_: str, directory_node: MemoryNode, artifacts: list[SourceArtifact]) -> MemoryNode:
    directory = str(directory_node.properties.get("relative_path") or directory_node.label or "")
    files = sorted(_normalize_relative_path(artifact.relative_path) for artifact in artifacts)
    confidence = _orphan_directory_confidence(directory)
    cleanup_priority = _orphan_directory_cleanup_priority(directory)
    reason = (
        f"Directory {directory} contains {len(files)} code artifacts with no detected inbound imports, calls, "
        "references, document links, or tests from outside the directory. Review the directory as one cleanup candidate instead of inspecting each file separately."
    )
    properties = {
        "project_id": project_id_,
        "relative_path": directory,
        "context_scope": "code",
        "finding_type": ORPHAN_DIRECTORY_FINDING_TYPE,
        "category": "dead_code",
        "severity": "info",
        "reason": reason,
        "evidence_scope": "directory_reachability",
        "confidence": confidence,
        "cleanup_priority": cleanup_priority,
        "cleanup_rank": {"high": 3, "medium": 2, "low": 1}[cleanup_priority],
        "removal_safety": "validate",
        "removal_reason": "Directory has no detected inbound project usage, but directory removal needs entrypoint, plugin, script, and dynamic-reference validation.",
        "validation_reason": "Validate manual entrypoints, plugin loading, external users, scripts, and dynamic imports before deleting this directory.",
        "blocking_signals": ["directory_level", "dynamic_reference_unknown", "entrypoint_unknown"],
        "symbol_id": directory_node.id,
        "symbol_type": "Directory",
        "symbol_kind": "directory",
        "symbol_name": directory,
        "qualified_name": directory,
        "name": directory,
        "directory": directory,
        "file_count": len(files),
        "files": files,
        "mode": "compile",
        "is_technical": True,
        "is_semantic": False,
    }
    return MemoryNode(
        id=stable_id("static-analysis-finding", project_id_, ORPHAN_DIRECTORY_FINDING_TYPE, directory),
        type="StaticAnalysisFinding",
        label=f"{ORPHAN_DIRECTORY_FINDING_TYPE}: {directory}",
        text=reason,
        canonical_key=f"{project_id_}:finding:{ORPHAN_DIRECTORY_FINDING_TYPE}:{directory}",
        properties=properties,
        salience=0.16,
        confidence=confidence,
        status="active",
    )


def _orphan_directory_finding_edge(project_id_: str, directory_node: MemoryNode, finding: MemoryNode) -> MemoryEdge:
    directory = str(finding.properties.get("relative_path") or "")
    return MemoryEdge(
        id=stable_id("edge", directory_node.id, "HAS_FINDING", finding.id),
        from_id=directory_node.id,
        to_id=finding.id,
        type="HAS_FINDING",
        origin="deterministic",
        properties={
            "project_id": project_id_,
            "relative_path": directory,
            "finding_type": ORPHAN_DIRECTORY_FINDING_TYPE,
            "source_file": directory,
            "extractor": "orphan_directory_analyzer",
            "evidence": str(finding.properties.get("reason") or finding.label or ""),
            "mode": "compile",
            "is_technical": True,
            "is_semantic": False,
        },
    )


def _node_relative_path(node: MemoryNode) -> str | None:
    value = node.properties.get("relative_path") or node.properties.get("source_file")
    if value is None:
        return None
    normalized = _normalize_relative_path(str(value))
    return normalized or None


def _normalize_relative_path(path: str) -> str:
    return str(path or "").replace("\\", "/").strip("/")


def _path_is_inside_directory(path: str, directory: str) -> bool:
    normalized_path = _normalize_relative_path(path)
    normalized_directory = _normalize_relative_path(directory)
    return normalized_path == normalized_directory or normalized_path.startswith(f"{normalized_directory}/")


def _edge_targets_directory_by_module(edge: MemoryEdge, directory: str) -> bool:
    module = str(edge.properties.get("module") or "").strip()
    if not module:
        return False
    module_path = module.replace(".", "/").strip("/")
    normalized_directory = _normalize_relative_path(directory)
    return module_path == normalized_directory or module_path.startswith(f"{normalized_directory}/")


def _resolve_import_relative_path_from_index(files_by_path: dict[str, MemoryNode], source_relative_path: str | None, module: str, level: int = 0) -> str:
    for candidate in _candidate_import_relative_paths(source_relative_path, module, level):
        if candidate in files_by_path:
            return candidate
    return ""


def _candidate_import_relative_paths(source_relative_path: str | None, module: str, level: int = 0) -> list[str]:
    module = str(module or "").strip(".")
    if not module or module == "*":
        return []
    source_path = Path(_normalize_relative_path(source_relative_path or ""))
    source_dir = "" if str(source_path.parent) == "." else source_path.parent.as_posix()
    module_parts = [part for part in module.split(".") if part]
    base_dirs: list[str] = []
    if level > 0:
        source_parts = [] if not source_dir else source_dir.split("/")
        keep = max(0, len(source_parts) - (level - 1))
        base_dirs.append("/".join(source_parts[:keep]))
    else:
        base_dirs.append("")
        if source_dir:
            base_dirs.append(source_dir)

    candidates: list[str] = []
    seen: set[str] = set()
    for base_dir in base_dirs:
        module_path = "/".join(part for part in [base_dir, *module_parts] if part)
        for candidate in (f"{module_path}.py", f"{module_path}/__init__.py"):
            normalized = _normalize_relative_path(candidate)
            if normalized and normalized not in seen:
                seen.add(normalized)
                candidates.append(normalized)
    return candidates


class _Aggregate:
    def __init__(self) -> None:
        self.added_nodes: set[str] = set()
        self.updated_nodes: set[str] = set()
        self.archived_nodes: set[str] = set()
        self.added_edges: set[str] = set()
        self.updated_edges: set[str] = set()
        self.archived_edges: set[str] = set()
        self.affected_node_ids: set[str] = set()
        self.affected_edge_ids: set[str] = set()
        self.affected_community_ids: set[str] = set()

    def add(self, result: ArtifactCompilationResult) -> None:
        self.added_nodes.update(result.added_nodes)
        self.updated_nodes.update(result.updated_nodes)
        self.archived_nodes.update(result.archived_nodes)
        self.added_edges.update(result.added_edges)
        self.updated_edges.update(result.updated_edges)
        self.archived_edges.update(result.archived_edges)
        self.affected_node_ids.update(result.affected_node_ids)
        self.affected_edge_ids.update(result.affected_edge_ids)
        self.affected_community_ids.update(result.affected_community_ids)
