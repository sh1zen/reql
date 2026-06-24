"""Project-level Markdown report orchestration."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..analysis.communities import CommunityDetector
from ..analysis.hubs import HubAnalyzer
from ..analysis.specificity import SpecificityScorer
from ..artifacts.cache import ArtifactCache, artifact_cache_path
from ..artifacts.fingerprint import normalize_path, project_id
from ..artifacts.scanner import ProjectScanner
from ..domain.constants import ACTIVE_STATUSES
from ..domain.models import MemoryEdge, MemoryNode
from ..domain.timeutils import utcnow_iso
from ..storage.graph_store import GraphStore
from .cache_report import CacheReportBuilder
from .delta_report import DeltaReportBuilder
from .graph_report import GraphReportBuilder


@dataclass(frozen=True, slots=True)
class ProjectReportFiles:
    graph_report: Path
    graph_deltas: Path
    cache_report: Path

    def to_dict(self) -> dict[str, str]:
        return {
            "graph_report": str(self.graph_report),
            "graph_deltas": str(self.graph_deltas),
            "cache_report": str(self.cache_report),
        }


class ProjectReportGenerator:
    """Builds deterministic REQL project reports from graph data."""

    def __init__(self, store: GraphStore) -> None:
        self.store = store

    def generate(self, path: str | Path) -> dict[str, str]:
        context = self._context(path)
        return {
            "GRAPH_REPORT.md": GraphReportBuilder().render(context),
            "GRAPH_DELTAS.md": DeltaReportBuilder().render(context),
            "CACHE_REPORT.md": CacheReportBuilder().render(context),
        }

    def write_reports(self, path: str | Path, *, output_dir: str | Path) -> ProjectReportFiles:
        reports = self.generate(path)
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        graph_report = out / "GRAPH_REPORT.md"
        graph_deltas = out / "GRAPH_DELTAS.md"
        cache_report = out / "CACHE_REPORT.md"
        graph_report.write_text(reports["GRAPH_REPORT.md"], encoding="utf-8")
        graph_deltas.write_text(reports["GRAPH_DELTAS.md"], encoding="utf-8")
        cache_report.write_text(reports["CACHE_REPORT.md"], encoding="utf-8")
        return ProjectReportFiles(graph_report=graph_report, graph_deltas=graph_deltas, cache_report=cache_report)

    def _context(self, path: str | Path) -> dict[str, Any]:
        root = normalize_path(path)
        project = self.store.get_node_by_key("Project", root)
        project_id_ = project.id if project else project_id(root)
        artifacts = _nodes(self.store, "SourceArtifact", project_id_)
        fragments = _nodes(self.store, "SourceFragment", project_id_)
        cache_entries = _nodes(self.store, "ArtifactCacheEntry", project_id_)
        runs = _nodes(self.store, "CompilationRun", project_id_)
        deltas = _nodes(self.store, "GraphDelta", project_id_)
        code_types = {
            "Module",
            "Class",
            "Function",
            "Method",
            "Variable",
            "Import",
            "Dependency",
            "Endpoint",
            "Schema",
            "Config",
            "Test",
            "StaticAnalysisFinding",
            "CodeSymbol",
        }
        code_nodes = [node for type_ in sorted(code_types) for node in _nodes(self.store, type_, project_id_, limit=10000)]
        call_edges = _project_edges(self.store, "CALLS", project_id_)

        scan_status: dict[str, object] | None = None
        try:
            scan = ProjectScanner().scan(path)
            scan_status = ArtifactCache(self.store, cache_path=artifact_cache_path(root)).status_for_scan(scan)
            skipped_files = scan.skipped_files
        except Exception:
            skipped_files = []

        try:
            community_result = CommunityDetector(self.store).detect(project_id=project_id_, limit=20)
            communities = community_result.community_nodes
        except Exception:
            communities = _nodes(self.store, "Community", project_id_)[:20]

        try:
            hub_report = HubAnalyzer(self.store).analyze(project_id=project_id_, limit=20, ensure_communities=False)
            hubs = hub_report.hubs
            hub_warnings = hub_report.warnings
        except Exception:
            hubs = []
            hub_warnings = []

        generic_warnings = _generic_high_degree(self.store, project_id=project_id_)
        archived_nodes = [
            *self.store.find_nodes_by_property("project_id", project_id_, status="archived", limit=1000),
            *self.store.find_nodes_by_property("project_id", None, status="archived", limit=1000),
        ]
        stale_nodes = _stale_nodes(
            self.store.find_nodes_by_property("project_id", project_id_, status=sorted(ACTIVE_STATUSES), limit=1000),
            project_id_,
        )

        return {
            "generated_at": utcnow_iso(),
            "root_path": root,
            "project_id": project_id_,
            "project": project,
            "artifacts": artifacts,
            "fragments": fragments,
            "cache_entries": cache_entries,
            "runs": sorted(runs, key=lambda n: str(n.properties.get("started_at") or n.created_at), reverse=True),
            "deltas": sorted(deltas, key=lambda n: str(n.properties.get("created_at") or n.created_at), reverse=True),
            "code_nodes": code_nodes,
            "call_edges": call_edges,
            "communities": communities,
            "hubs": hubs,
            "hub_warnings": hub_warnings,
            "generic_warnings": generic_warnings,
            "archived_nodes": archived_nodes,
            "stale_nodes": stale_nodes,
            "scan_status": scan_status,
            "skipped_files": skipped_files,
        }


def _nodes(store: GraphStore, type_: str, project_id_: str, *, limit: int = 100000) -> list[MemoryNode]:
    return store.find_nodes_by_property("project_id", project_id_, type_=type_, limit=limit)


def _project_edges(store: GraphStore, type_: str, project_id_: str) -> list[MemoryEdge]:
    return [
        edge
        for edge in store.get_edges(type_=type_, limit=100000)
        if edge.properties.get("project_id") in {project_id_, None}
    ]


def _stale_nodes(nodes: list[MemoryNode], project_id_: str) -> list[MemoryNode]:
    scoped = [
        node
        for node in nodes
        if node.status not in {"archived", "deleted", "rejected"} and node.properties.get("project_id") in {project_id_, None}
    ]
    scoped.sort(key=lambda node: (node.last_used_at or "", node.updated_at))
    return scoped[:20]


def _generic_high_degree(store: GraphStore, *, project_id: str) -> list[tuple[MemoryNode, int, float]]:
    scorer = SpecificityScorer(store)
    scored: list[tuple[MemoryNode, int, float]] = []
    candidates = store.top_nodes_by_degree(
        limit=200,
        statuses=set(ACTIVE_STATUSES),
        exclude_types={"Community", "CompilationRun", "GraphDelta", "ArtifactCacheEntry"},
        ignored_edge_types={"BELONGS_TO_COMMUNITY", "BRIDGES_COMMUNITY"},
        project_id=project_id,
        include_global_project=True,
    )
    for node, degree, _ in candidates:
        if degree < 3:
            continue
        specificity = scorer.score(node)
        if specificity.generic_penalty >= 0.35:
            scored.append((node, degree, specificity.generic_penalty))
    scored.sort(key=lambda item: (item[2], item[1]), reverse=True)
    return scored[:10]
