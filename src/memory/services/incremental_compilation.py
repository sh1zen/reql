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
from ..domain.models import MemoryNode
from ..domain.timeutils import utcnow_iso
from ..storage.graph_store import GraphStore


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
        with (profile.span("compile.refresh_context_scope", artifacts=len(scan.artifacts)) if profile else nullcontext()):
            aggregate.add(self._refresh_context_scope_metadata(scan.artifacts))
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

        for artifact_id in sorted(dirty.deleted_artifact_ids):
            node = self.store.get_node(artifact_id)
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

        run.nodes_created = len(aggregate.added_nodes)
        run.nodes_updated = len(aggregate.updated_nodes)
        run.edges_created = len(aggregate.added_edges)
        run.edges_updated = len(aggregate.updated_edges)
        run.completed_at = utcnow_iso()
        run.status = "failed" if run.errors else "completed"

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

    def _refresh_context_scope_metadata(self, artifacts: list[SourceArtifact]) -> ArtifactCompilationResult:
        result = ArtifactCompilationResult(artifact_id="*")
        for artifact in artifacts:
            scope = artifact_context_scope(artifact)
            nodes_by_id: dict[str, MemoryNode] = {}
            artifact_node = self.store.get_node(artifact.id)
            if artifact_node is not None:
                nodes_by_id[artifact_node.id] = artifact_node
            for node in self.store.find_nodes_by_property("artifact_id", artifact.id, limit=100000):
                if node.type == "ArtifactCacheEntry":
                    continue
                nodes_by_id[node.id] = node
            for node in nodes_by_id.values():
                if node.status == "archived":
                    continue
                if node.properties.get("context_scope") == scope:
                    continue
                properties = dict(node.properties)
                properties["context_scope"] = scope
                properties["updated_at"] = utcnow_iso()
                self.store.update_node_fields(node.id, properties=properties)
                result.updated_nodes.append(node.id)
                result.affected_node_ids.add(node.id)
        return result

    def _archive_deleted_artifact_node(self, node_id: str) -> None:
        node = self.store.get_node(node_id)
        if node is None or node.status == "archived":
            return
        properties = dict(node.properties)
        properties["status"] = "archived"
        properties["updated_at"] = utcnow_iso()
        self.store.update_node_fields(node.id, status="archived", properties=properties)

    def _archive_file_nodes(self, project_id_: str, artifact_id: str) -> ArtifactCompilationResult:
        result = ArtifactCompilationResult(artifact_id=artifact_id)
        for node in self.store.find_nodes_by_property("artifact_id", artifact_id, type_="File", limit=1000):
            if node.properties.get("project_id") != project_id_ or node.status == "archived":
                continue
            properties = dict(node.properties)
            properties["status"] = "archived"
            properties["updated_at"] = utcnow_iso()
            self.store.update_node_fields(node.id, status="archived", properties=properties)
            result.archived_nodes.append(node.id)
            result.affected_node_ids.add(node.id)
            for edge in self.store.incident_edges([node.id], limit=10000):
                if edge.properties.get("artifact_id") != artifact_id or edge.properties.get("status") == "archived":
                    continue
                edge_properties = dict(edge.properties)
                edge_properties["status"] = "archived"
                self.store.update_edge_fields(edge.id, properties=edge_properties)
                result.archived_edges.append(edge.id)
                result.affected_edge_ids.add(edge.id)
        return result

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
