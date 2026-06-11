"""Application facade for the graph-native memory layer."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator, TypeVar

from memory.config import REQLConfig, default_config
from memory.diagnostics import PerformanceLogger
from memory.domain.exceptions import StorageError
from memory.domain.models import (
    ActivationOptions,
    ActivationResult,
    MemoryEdge,
    MemoryNode,
    MemoryQuery,
    MemorySubgraph,
)
from memory.engines.activation import ActivationEngine
from memory.engines.salience import SalienceEngine
from memory.extraction.deterministic import DeterministicExtractor
from memory.infrastructure.block import BlockGraphStore
from memory.ports.extractor import SemanticExtractor
from memory.ports.graph_store import GraphStore
from memory.reporting.html_graph import write_graph_html
from memory.reporting.project_report import ProjectReportFiles, ProjectReportGenerator
from memory.analysis.communities import CommunityResult
from memory.analysis.hubs import HubReport
from memory.artifacts.project import ProjectRegistry
from memory.artifacts.compiler import ArtifactCompiler
from memory.services.incremental_compilation import CompileProjectResult, IncrementalCompilationService
from memory.services.project_watch import ProjectWatchEvent, ProjectWatchService
from memory.services.retrieval import RetrievalEngine


MemoryGraphT = TypeVar("MemoryGraphT", bound="MemoryGraph")


def _ensure_readable_storage_payload(path: Path) -> None:
    wal_path = path.with_name(f"{path.name}.wal")
    try:
        has_store_payload = path.exists() and path.stat().st_size > 0
        has_wal_payload = wal_path.exists() and wal_path.stat().st_size > 0
    except OSError as exc:
        raise StorageError(f"Cannot inspect REQL storage at {path}: {exc}") from exc
    if not has_store_payload and not has_wal_payload:
        raise StorageError(f"Cannot open missing REQL storage in read-only mode: {path}")


class MemoryGraph:
    """Stable public facade over the memory subsystem.

    The facade wires the domain services without leaking storage details. The
    bundled ``open`` constructor uses ``BlockGraphStore`` for local persistence,
    but callers may inject any ``GraphStore`` implementation that satisfies the
    port.
    """

    def __init__(
        self,
        store: GraphStore,
        extractor: SemanticExtractor | None = None,
        *,
        config: REQLConfig | None = None,
        profile_logger: PerformanceLogger | None = None,
    ) -> None:
        self.store = store
        self.config = config or default_config()
        self.profile_logger = profile_logger
        self.extractor = extractor or DeterministicExtractor()
        self.activation = ActivationEngine(store)
        self.retrieval = RetrievalEngine(store, extractor or DeterministicExtractor(), profile_logger=profile_logger)
        self.salience = SalienceEngine(store)
        self.project_reporter = ProjectReportGenerator(store)
        self.projects = ProjectRegistry(store)
        self.incremental = IncrementalCompilationService(
            store,
            compiler=ArtifactCompiler(),
            profile_logger=profile_logger,
        )
        self.project_watcher = ProjectWatchService(self.incremental)
        from memory.analysis.communities import CommunityDetector
        from memory.analysis.hubs import HubAnalyzer

        self.community_detector = CommunityDetector(store)
        self.hub_analyzer = HubAnalyzer(store)

    @classmethod
    def open(
        cls: type[MemoryGraphT],
        path: str | Path,
        *,
        extractor: SemanticExtractor | None = None,
        config: REQLConfig | None = None,
        profile_logger: PerformanceLogger | None = None,
        read_only: bool = False,
    ) -> MemoryGraphT:
        storage_path = Path(path).expanduser()
        if read_only:
            _ensure_readable_storage_payload(storage_path)
        store = BlockGraphStore(storage_path, read_only=read_only)
        try:
            return cls(store, extractor=extractor, config=config, profile_logger=profile_logger)
        except Exception:
            store.close()
            raise

    def enable_profile_log(self, path: str | Path, *, command: str | None = None) -> PerformanceLogger:
        logger = PerformanceLogger(path, command=command)
        self.profile_logger = logger
        self.retrieval.profile_logger = logger
        self.incremental.profile_logger = logger
        return logger

    def close(self) -> None:
        if self.profile_logger:
            with self.profile_logger.span("graph.close"):
                self.store.close()
            return
        self.store.close()

    def retrieve(
        self,
        text: str,
        *,

        top_k: int = 20,
        max_depth: int = 3,
        min_activation: float = 0.03,
        include_archived: bool = False,
        node_types: set[str] | None = None,
        edge_types: set[str] | None = None,
    ) -> MemorySubgraph:
        """Return the raw retrieved subgraph for callers that need graph records."""
        return self.retrieval.retrieve(
            MemoryQuery(
                text=text,
                top_k=top_k,
                max_depth=max_depth,
                min_activation=min_activation,
                include_archived=include_archived,
                node_types=node_types,
                edge_types=edge_types,
            )
        )

    def compose_context(
        self,
        text: str,
        *,

        top_k: int = 20,
        max_depth: int = 3,
        max_items: int = 18,
        include_archived: bool = False,
    ) -> str:
        subgraph = self.retrieval.retrieve(
            MemoryQuery(
                text=text,

                top_k=top_k,
                max_depth=max_depth,
                include_archived=include_archived,
            )
        )
        return self.retrieval.compose_context(subgraph, max_items=max_items)

    def query_graph(
        self,
        text: str,
        *,

        top_k: int = 12,
        max_depth: int = 2,
        max_nodes: int = 80,
        max_edges: int = 160,
        max_sources: int = 20,
        max_items: int = 18,
        filter_generic: bool = True,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        return self.retrieval.query_graph(
            MemoryQuery(
                text=text,

                top_k=top_k,
                max_depth=max_depth,
                include_archived=include_archived,
            ),
            max_nodes=max_nodes,
            max_edges=max_edges,
            max_sources=max_sources,
            max_items=max_items,
            filter_generic=filter_generic,
        )

    def query_explore(
        self,
        text: str,
        *,
        views: list[str] | None = None,
        top_k: int = 12,
        max_depth: int = 3,
        limit: int = 12,
        max_items: int = 18,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        """Return dependency-oriented query slices for coding agents."""
        return self.retrieval.query_explore(
            MemoryQuery(
                text=text,

                top_k=top_k,
                max_depth=max_depth,
                include_archived=include_archived,
            ),
            views=views,
            limit=limit,
            max_items=max_items,
        )

    def query_context(
        self,
        text: str,
        *,

        top_k: int = 20,
        max_depth: int = 3,
        max_items: int = 20,
        mode: str = "informative",
        scopes: list[str] | set[str] | tuple[str, ...] | None = None,
        include_archived: bool = False,
    ) -> str:
        """Return the compact deterministic context block for a query."""
        subgraph = self.retrieval.retrieve(
            MemoryQuery(
                text=text,
                top_k=top_k,
                max_depth=max_depth,
                include_archived=include_archived,
                context_scopes=set(scopes) if scopes else None,
            )
        )
        return self.retrieval.compose_context(
            subgraph,
            max_items=max_items,
            query_mode=mode,
            query_scopes=scopes,
        )

    def query_context_payload(
        self,
        text: str,
        *,

        top_k: int = 20,
        max_depth: int = 3,
        max_items: int = 20,
        mode: str = "informative",
        scopes: list[str] | set[str] | tuple[str, ...] | None = None,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        """Return compact structured query context."""
        subgraph = self.retrieval.retrieve(
            MemoryQuery(
                text=text,
                top_k=top_k,
                max_depth=max_depth,
                include_archived=include_archived,
                context_scopes=set(scopes) if scopes else None,
            )
        )
        return self.retrieval.query_context_payload(subgraph, max_items=max_items, query_mode=mode, query_scopes=scopes)

    def query_memories(
        self,
        text: str,
        *,

        top_k: int = 12,
        max_depth: int = 2,
        limit: int = 12,
        include_sources: bool = True,
        filter_generic: bool = True,
        max_text_chars: int = 600,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        return self.retrieval.query_memories(
            MemoryQuery(
                text=text,

                top_k=top_k,
                max_depth=max_depth,
                include_archived=include_archived,
            ),
            limit=limit,
            include_sources=include_sources,
            filter_generic=filter_generic,
            max_text_chars=max_text_chars,
        )

    def query_memories_payload(
        self,
        text: str,
        *,

        top_k: int = 12,
        max_depth: int = 2,
        limit: int = 12,
        include_sources: bool = True,
        filter_generic: bool = True,
        max_text_chars: int = 600,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        return self.retrieval.query_memories_payload(
            MemoryQuery(
                text=text,

                top_k=top_k,
                max_depth=max_depth,
                include_archived=include_archived,
            ),
            limit=limit,
            include_sources=include_sources,
            filter_generic=filter_generic,
            max_text_chars=max_text_chars,
        )

    def activate(
        self,
        seed_node_ids: list[str],
        *,

        max_depth: int = 3,
        min_activation: float = 0.03,
    ) -> ActivationResult:
        return self.activation.activate(
            seed_node_ids,
            ActivationOptions(max_depth=max_depth, min_activation=min_activation),
        )

    def project_report(self, path: str | Path, *, output_dir: str | Path) -> ProjectReportFiles:
        return self.project_reporter.write_reports(path, output_dir=output_dir)

    def export_json(self) -> dict[str, Any]:
        return self.store.export_json()

    def export_html(self, output_path: str | Path) -> Path:
        return write_graph_html(self.export_json(), output_path)

    def query(self, statement: str):
        """Execute a REQL statement.

        REQL is parsed into an AST and evaluated against the graph facade, so the
        public query layer remains independent from the physical storage adapter.
        """
        from memory.query.evaluator import execute_reql

        if self.profile_logger:
            with self.profile_logger.span("query.total", statement_length=len(statement)):
                return execute_reql(self, statement)
        return execute_reql(self, statement)

    def project_status(self, path: str | Path) -> dict[str, Any] | None:
        return self.projects.project_status(path)

    def compile_project(
        self,
        path: str | Path,
        *,

        max_file_size_bytes: int = 10 * 1024 * 1024,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        cache_enabled: bool = True,
        parsing_options: dict[str, object] | None = None,
    ) -> CompileProjectResult:
        if parsing_options is None:
            parsing_options = self._compile_parsing_options()
        return self.incremental.compile_path(
            path,

            max_file_size_bytes=max_file_size_bytes,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
            cache_enabled=cache_enabled,
            parsing_options=parsing_options,
        )

    def update_project(
        self,
        path: str | Path,
        *,

        max_file_size_bytes: int = 10 * 1024 * 1024,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        cache_enabled: bool = True,
        parsing_options: dict[str, object] | None = None,
    ) -> CompileProjectResult:
        """Incrementally update a project through the compile pipeline."""
        return self.compile_project(
            path,

            max_file_size_bytes=max_file_size_bytes,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
            cache_enabled=cache_enabled,
            parsing_options=parsing_options,
        )

    def watch_project(
        self,
        path: str | Path,
        *,

        max_file_size_bytes: int = 10 * 1024 * 1024,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        cache_enabled: bool = True,
        parsing_options: dict[str, object] | None = None,
        interval_seconds: float = 0.5,
        debounce_seconds: float = 0.1,
        max_iterations: int | None = None,
    ) -> Iterator[ProjectWatchEvent]:
        """Yield project watch events and compile dirty files automatically."""
        if parsing_options is None:
            parsing_options = self._compile_parsing_options()
        return self.project_watcher.watch_path(
            path,

            max_file_size_bytes=max_file_size_bytes,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
            cache_enabled=cache_enabled,
            parsing_options=parsing_options,
            interval_seconds=interval_seconds,
            debounce_seconds=debounce_seconds,
            max_iterations=max_iterations,
        )

    def watch_project_once(
        self,
        path: str | Path,
        *,

        max_file_size_bytes: int = 10 * 1024 * 1024,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        cache_enabled: bool = True,
        parsing_options: dict[str, object] | None = None,
    ) -> ProjectWatchEvent:
        """Poll once and compile dirty project artifacts when needed."""
        if parsing_options is None:
            parsing_options = self._compile_parsing_options()
        return self.project_watcher.poll_once(
            path,

            max_file_size_bytes=max_file_size_bytes,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
            cache_enabled=cache_enabled,
            parsing_options=parsing_options,
        )

    def cache_status(
        self,
        path: str | Path,
        *,

        max_file_size_bytes: int = 10 * 1024 * 1024,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        cache_enabled: bool = True,
        parsing_options: dict[str, object] | None = None,
    ) -> dict[str, object]:
        if parsing_options is None:
            parsing_options = self._compile_parsing_options()
        return self.incremental.cache_status(
            path,

            max_file_size_bytes=max_file_size_bytes,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
            cache_enabled=cache_enabled,
            parsing_options=parsing_options,
        )

    def clear_cache(self, path: str | Path) -> dict[str, object]:
        return self.incremental.clear_cache(path)

    def list_deltas(self, *, limit: int = 20):
        return self.incremental.list_deltas(limit=limit)

    def show_delta(self, delta_id: str):
        return self.incremental.show_delta(delta_id)

    def detect_communities(
        self,

        project_id: str | None = None,
        options: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> CommunityResult:
        return self.community_detector.detect(project_id=project_id, options=options, limit=limit)

    def analyze_hubs(
        self,

        project_id: str | None = None,
        limit: int = 20,
        node_types: set[str] | None = None,
    ) -> HubReport:
        return self.hub_analyzer.analyze(project_id=project_id, limit=limit, node_types=node_types)

    def explain_hub(self, node_id: str):
        return self.hub_analyzer.explain(node_id)

    def add_node(self, node: MemoryNode) -> tuple[MemoryNode, bool]:
        return self.store.upsert_node(node)

    def add_edge(self, edge: MemoryEdge) -> tuple[MemoryEdge, bool]:
        return self.store.upsert_edge(edge)

    def get_node(self, node_id: str) -> MemoryNode | None:
        return self.store.get_node(node_id)

    def get_edges(self, **kwargs: Any) -> list[MemoryEdge]:
        return self.store.get_edges(**kwargs)

    def inspect_node(
        self,
        node_id: str,
        *,

        limit: int = 30,
    ) -> dict[str, Any]:
        """Return a node, its local graph context, and source/location hints."""
        node = self.store.get_node(node_id)
        if node is None:
            return {"id": node_id, "found": False, "node": None, "location": None, "sources": [], "neighbors": [], "outgoing": [], "incoming": []}

        limit = max(1, int(limit))
        outgoing = self.store.get_edges(from_id=node_id, limit=limit)
        incoming = self.store.get_edges(to_id=node_id, limit=limit)
        adjacent_ids = {edge.to_id for edge in outgoing}
        adjacent_ids.update(edge.from_id for edge in incoming)
        adjacent = {item.id: item for item in self.store.get_nodes(sorted(adjacent_ids))}
        sources = self._inspect_sources(node, outgoing, incoming, adjacent)
        neighbors = [
            self._inspect_neighbor_payload(edge, adjacent.get(edge.to_id), direction="outgoing")
            for edge in outgoing
        ]
        neighbors.extend(
            self._inspect_neighbor_payload(edge, adjacent.get(edge.from_id), direction="incoming")
            for edge in incoming
        )
        return {
            "id": node_id,
            "found": True,
            "node": node.to_dict(),
            "location": self._location_payload(node),
            "sources": sources,
            "neighbors": neighbors,
            "outgoing": [edge.to_dict() for edge in outgoing],
            "incoming": [edge.to_dict() for edge in incoming],
        }

    def _inspect_sources(
        self,
        node: MemoryNode,
        outgoing: list[MemoryEdge],
        incoming: list[MemoryEdge],
        adjacent: dict[str, MemoryNode],
    ) -> list[dict[str, Any]]:
        sources: dict[str, dict[str, Any]] = {}
        for item in (node, *adjacent.values()):
            location = self._location_payload(item)
            if location is None:
                continue
            key = str(location.get("artifact_id") or location.get("path") or location.get("relative_path") or item.id)
            sources.setdefault(
                key,
                {
                    "node_id": item.id,
                    "node_type": item.type,
                    "label": item.label,
                    "location": location,
                    "via_edges": [],
                },
            )
        for edge in [*outgoing, *incoming]:
            location = self._location_payload(edge)
            if location is None:
                continue
            key = str(location.get("artifact_id") or location.get("path") or location.get("relative_path") or edge.id)
            sources.setdefault(
                key,
                {
                    "node_id": None,
                    "node_type": None,
                    "label": location.get("relative_path") or location.get("path"),
                    "location": location,
                    "via_edges": [],
                },
            )
            sources[key]["via_edges"].append(edge.id)
        return list(sources.values())

    def _inspect_neighbor_payload(
        self,
        edge: MemoryEdge,
        other: MemoryNode | None,
        *,
        direction: str,
    ) -> dict[str, Any]:
        return {
            "edge_id": edge.id,
            "edge_type": edge.type,
            "direction": direction,
            "from_id": edge.from_id,
            "to_id": edge.to_id,
            "other_id": edge.to_id if direction == "outgoing" else edge.from_id,
            "other_type": other.type if other is not None else None,
            "other_label": (other.label or other.text or other.canonical_key or other.id) if other is not None else None,
            "weight": edge.weight,
            "confidence": edge.confidence,
            "location": self._location_payload(edge),
        }

    @staticmethod
    def _location_payload(item: MemoryNode | MemoryEdge) -> dict[str, Any] | None:
        props = dict(item.properties)
        metadata = props.get("metadata")
        if isinstance(metadata, dict):
            for key in ("source_path", "path", "relative_path", "source_file", "source_url", "url"):
                if key not in props and metadata.get(key) is not None:
                    props[key] = metadata.get(key)
            for key in ("line_start", "start_line", "line_end", "end_line"):
                if key not in props and metadata.get(key) is not None:
                    props[key] = metadata.get(key)
        path = props.get("path") or props.get("source_path")
        relative_path = props.get("relative_path") or props.get("source_file")
        source_url = props.get("source_url") or props.get("url")
        line_start = props.get("line_start", props.get("start_line"))
        line_end = props.get("line_end", props.get("end_line"))
        section = props.get("section_path") or props.get("section")
        artifact_id = props.get("artifact_id")
        if not any(value is not None and value != "" for value in (path, relative_path, source_url, line_start, line_end, section, artifact_id)):
            return None
        return {
            "path": path,
            "relative_path": relative_path,
            "source_url": source_url,
            "line_start": line_start,
            "line_end": line_end,
            "section": section,
            "artifact_id": artifact_id,
        }

    def _compile_parsing_options(self) -> dict[str, object]:
        return {"compile": self.config.compile.to_dict()}

