"""Document-aware source artifact compiler for incremental graph updates."""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
import re
from typing import Any

from ..code_analysis.language_detection import detect_code_language
from ..code_analysis.models import CodeImport, CodeModule, CodeParseResult, CodeSymbol, CodeText
from ..code_analysis.parser_base import CodeParserRegistry, default_code_parser_registry
from ..code_analysis.symbol_table import SymbolTable
from ..document_ingestion.base import ParserRegistry, default_parser_registry
from ..document_ingestion.metadata import make_fragment
from ..document_ingestion.models import DocumentFragment, DocumentParseResult
from ..domain.ids import stable_id
from ..domain.models import MemoryEdge, MemoryNode
from ..domain.timeutils import utcnow_iso
from ..extraction.document_processor import DocumentProcessingResult, DocumentProcessor, DocumentRawEvent, DocumentTerm, DocumentTermRelation
from ..extraction.normalization import canonicalize
from ..ports.graph_store import GraphStore
from .context_scope import artifact_context_scope
from .models import SourceArtifact


@dataclass(slots=True)
class ArtifactCompilationResult:
    artifact_id: str
    added_nodes: list[str] = field(default_factory=list)
    updated_nodes: list[str] = field(default_factory=list)
    archived_nodes: list[str] = field(default_factory=list)
    added_edges: list[str] = field(default_factory=list)
    updated_edges: list[str] = field(default_factory=list)
    archived_edges: list[str] = field(default_factory=list)
    affected_node_ids: set[str] = field(default_factory=set)
    affected_edge_ids: set[str] = field(default_factory=set)
    affected_community_ids: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)


DOCUMENT_CODE_LINK_NODE_TYPES = {"Module", "Function", "Class", "Interface", "Method", "Endpoint", "Schema"}
DOCUMENT_CODE_LINK_STOP_TERMS = {
    "agent",
    "agents",
    "api",
    "app",
    "architecture",
    "cache",
    "cli",
    "code",
    "config",
    "data",
    "docs",
    "edge",
    "file",
    "files",
    "graph",
    "guide",
    "item",
    "lib",
    "main",
    "memory",
    "node",
    "parser",
    "path",
    "project",
    "query",
    "readme",
    "result",
    "src",
    "status",
    "store",
    "test",
    "text",
    "usage",
    "user",
    "users",
}
MAX_DOCUMENT_CODE_LINKS_PER_FRAGMENT = 8
MAX_DOCUMENT_CODE_LINKS_PER_RUN = 1000

LANGUAGE_BUILTIN_GLOBALS = {
    "AbortController",
    "AbortSignal",
    "Array",
    "Blob",
    "Boolean",
    "Date",
    "Error",
    "False",
    "File",
    "FormData",
    "Headers",
    "Intl",
    "JSON",
    "Map",
    "Math",
    "None",
    "Number",
    "Object",
    "Promise",
    "Proxy",
    "RangeError",
    "Reflect",
    "RegExp",
    "Request",
    "Response",
    "Set",
    "String",
    "Symbol",
    "TextDecoder",
    "TextEncoder",
    "True",
    "TypeError",
    "URL",
    "URLSearchParams",
    "WeakMap",
    "WeakSet",
    "abs",
    "all",
    "any",
    "bool",
    "bytes",
    "callable",
    "console",
    "dict",
    "dir",
    "enumerate",
    "filter",
    "float",
    "getattr",
    "hasattr",
    "hash",
    "id",
    "int",
    "isinstance",
    "iter",
    "len",
    "list",
    "map",
    "max",
    "min",
    "next",
    "object",
    "open",
    "parseFloat",
    "parseInt",
    "print",
    "range",
    "repr",
    "reversed",
    "round",
    "set",
    "setattr",
    "sorted",
    "str",
    "sum",
    "super",
    "tuple",
    "type",
    "vars",
    "zip",
}

CODE_GRAPH_NODE_TYPES = {
    "Module",
    "CodeSymbol",
    "Function",
    "Class",
    "Interface",
    "Method",
    "Variable",
    "Import",
    "Comment",
    "Docstring",
    "Endpoint",
    "Schema",
    "Config",
    "Test",
    "Dependency",
    "StaticAnalysisFinding",
}

CODE_GRAPH_EDGE_TYPES = {
    "DEFINES",
    "IMPORTS",
    "IMPORTS_FROM",
    "RE_EXPORTS",
    "CALLS",
    "REFERENCES",
    "READS",
    "WRITES",
    "RETURNS",
    "RAISES",
    "HAS_DOCSTRING",
    "HAS_COMMENT",
    "CONTAINS",
    "METHOD",
    "INHERITS",
    "IMPLEMENTS",
    "OVERRIDES",
    "INSTANTIATES",
    "DECORATED_BY",
    "HANDLES_ROUTE",
    "TESTS",
    "CONFIGURES",
    "HAS_FINDING",
    "DEPENDS_ON",
    "EVIDENCED_BY",
}


class ArtifactCompiler:
    """Compiles one artifact into parsed SourceFragment nodes and relations."""

    def __init__(
        self,
        *,
        parser_registry: ParserRegistry | None = None,
        code_parser_registry: CodeParserRegistry | None = None,
        enable_pdf: bool = True,
        ingest_documents: bool = True,
        document_policies: list[dict[str, object]] | None = None,
        document_processor: DocumentProcessor | None = None,
    ) -> None:
        self.enable_pdf = enable_pdf
        self.ingest_documents = ingest_documents
        self.document_policies = _normalize_document_policies(document_policies)
        self.document_processor = document_processor or DocumentProcessor()
        self.parser_registry = parser_registry or default_parser_registry(
            enable_pdf=enable_pdf,
        )
        self.code_parser_registry = code_parser_registry or default_code_parser_registry()

    def compile_artifact(self, store: GraphStore, artifact: SourceArtifact) -> ArtifactCompilationResult:
        code_result: CodeParseResult | None = None
        if _is_code_artifact(artifact) and type(self).build_fragments is ArtifactCompiler.build_fragments:
            code_text = self.read_code_artifact_text(artifact)
            code_result = self.parse_code_text(artifact, code_text)
            parse_result = _document_result_from_code(artifact, code_result, code_text)
        elif _is_document_artifact(artifact) and not self.document_ingest_enabled(artifact):
            parse_result = _skipped_document_parse_result(artifact)
        else:
            parse_result = self.build_fragments(artifact)
        result = ArtifactCompilationResult(artifact_id=artifact.id, errors=list(parse_result.errors))
        classification_result = self._persist_compile_classification(store, artifact)
        result.added_nodes.extend(classification_result.added_nodes)
        result.updated_nodes.extend(classification_result.updated_nodes)
        result.added_edges.extend(classification_result.added_edges)
        result.updated_edges.extend(classification_result.updated_edges)
        result.affected_node_ids.update(classification_result.affected_node_ids)
        result.affected_edge_ids.update(classification_result.affected_edge_ids)

        existing = _artifact_fragments(store, artifact)
        existing_ids = {node.id for node in existing}
        fragment_id_map: dict[str, str] = {}
        persisted_fragment_ids: set[str] = set()
        fragment_edges: list[MemoryEdge] = []

        fragment_nodes = [_fragment_node(artifact, fragment, parse_result) for fragment in parse_result.fragments]
        for fragment, (node, created) in zip(parse_result.fragments, store.batch_upsert_nodes(fragment_nodes)):
            fragment_id_map[fragment.id] = node.id
            persisted_fragment_ids.add(node.id)
            result.affected_node_ids.add(node.id)
            if created:
                result.added_nodes.append(node.id)
            elif node.id in existing_ids:
                result.updated_nodes.append(node.id)

            for edge in _fragment_edges(artifact, fragment, node.id):
                fragment_edges.append(edge)
        _upsert_edges_deduped(store, result, fragment_edges)

        relation_result = self._persist_document_relations(store, artifact, parse_result, fragment_id_map)
        result.added_nodes.extend(relation_result.added_nodes)
        result.updated_nodes.extend(relation_result.updated_nodes)
        result.added_edges.extend(relation_result.added_edges)
        result.updated_edges.extend(relation_result.updated_edges)
        result.affected_node_ids.update(relation_result.affected_node_ids)
        result.affected_edge_ids.update(relation_result.affected_edge_ids)

        if self.document_processing_enabled(artifact):
            processing_result = self._persist_document_processing(store, artifact, parse_result, fragment_id_map)
            result.added_nodes.extend(processing_result.added_nodes)
            result.updated_nodes.extend(processing_result.updated_nodes)
            result.archived_nodes.extend(processing_result.archived_nodes)
            result.added_edges.extend(processing_result.added_edges)
            result.updated_edges.extend(processing_result.updated_edges)
            result.archived_edges.extend(processing_result.archived_edges)
            result.affected_node_ids.update(processing_result.affected_node_ids)
            result.affected_edge_ids.update(processing_result.affected_edge_ids)
            parse_result.metadata["document_processor"] = {
                "status": "completed",
                "updated_at": utcnow_iso(),
            }

        if code_result is not None and code_result.parser_name != "none":
            code_graph_result = self._persist_code_graph(store, artifact, code_result, parse_result)
            result.added_nodes.extend(code_graph_result.added_nodes)
            result.updated_nodes.extend(code_graph_result.updated_nodes)
            result.archived_nodes.extend(code_graph_result.archived_nodes)
            result.added_edges.extend(code_graph_result.added_edges)
            result.updated_edges.extend(code_graph_result.updated_edges)
            result.archived_edges.extend(code_graph_result.archived_edges)
            result.affected_node_ids.update(code_graph_result.affected_node_ids)
            result.affected_edge_ids.update(code_graph_result.affected_edge_ids)

        for node in existing:
            if node.id in persisted_fragment_ids or node.status == "archived":
                continue
            for concept in _concepts_for_fragment(store, node):
                _archive_concept(store, concept)
                result.archived_nodes.append(concept.id)
                result.affected_node_ids.add(concept.id)
            _archive_fragment(store, node)
            result.archived_nodes.append(node.id)
            result.affected_node_ids.add(node.id)
            for edge in _fragment_related_edges(store, node):
                _archive_edge(store, edge)
                result.archived_edges.append(edge.id)
                result.affected_edge_ids.add(edge.id)

        _mark_artifact_compiled(store, artifact, parse_result)
        result.affected_node_ids.add(artifact.id)
        result.updated_nodes.append(artifact.id)
        return result

    def build_fragments(self, artifact: SourceArtifact) -> DocumentParseResult:
        content = Path(artifact.path).read_bytes()
        if artifact.artifact_type == "pdf" and not self.enable_pdf:
            return _disabled_parse_result(artifact, "pdf", "PDF parsing is disabled by compile.documents policy")
        parser = self.parser_registry.parser_for(artifact)
        return parser.parse(artifact, content)

    def parse_code_artifact(self, artifact: SourceArtifact) -> CodeParseResult:
        return self.parse_code_text(artifact, self.read_code_artifact_text(artifact))

    def read_code_artifact_text(self, artifact: SourceArtifact) -> str:
        return Path(artifact.path).read_text(encoding="utf-8-sig", errors="replace")

    def parse_code_text(self, artifact: SourceArtifact, text: str) -> CodeParseResult:
        language = detect_code_language(artifact)
        parser = self.code_parser_registry.parser_for(artifact)
        if parser is None:
            return _empty_code_result(
                artifact,
                None if language else f"No code parser available for language {artifact.language!r}",
            )
        return parser.parse_artifact(artifact, text)

    def document_ingest_enabled(self, artifact: SourceArtifact) -> bool:
        if not self.ingest_documents or not _is_document_artifact(artifact):
            return False
        policy = _document_policy_for_artifact(self.document_policies, artifact)
        if policy is None:
            return True
        return bool(policy.get("ingest", True))

    def document_processing_enabled(self, artifact: SourceArtifact) -> bool:
        return self.document_ingest_enabled(artifact)

    def _persist_document_relations(
        self,
        store: GraphStore,
        artifact: SourceArtifact,
        parse_result: DocumentParseResult,
        fragment_id_map: dict[str, str],
    ) -> ArtifactCompilationResult:
        result = ArtifactCompilationResult(artifact_id=artifact.id)
        fragments_by_id = {fragment_id_map.get(fragment.id, fragment.id): fragment for fragment in parse_result.fragments}
        current_heading_id: str | None = None
        concept_upserts: list[tuple[DocumentFragment, str, MemoryNode]] = []
        edges: list[MemoryEdge] = []

        for fragment in parse_result.fragments:
            fragment_id = fragment_id_map.get(fragment.id, fragment.id)
            if fragment.fragment_type == "heading":
                current_heading_id = fragment_id
                concept = _concept_node(artifact, fragment, parse_result)
                if concept is not None:
                    concept_upserts.append((fragment, fragment_id, concept))
            if fragment.fragment_type == "code_block":
                raw_parent_id = str(fragment.metadata.get("parent_heading_id") or "")
                parent_id = fragment_id_map.get(raw_parent_id, raw_parent_id) or current_heading_id or ""
                if parent_id and parent_id in fragments_by_id:
                    edge = _typed_edge(parent_id, fragment_id, "HAS_CODE_BLOCK", {"project_id": artifact.project_id, "artifact_id": artifact.id, "relative_path": artifact.relative_path}, source_file=artifact.relative_path, line_start=fragment.start_line, line_end=fragment.end_line, extractor=parse_result.parser_name, evidence=fragment.fragment_type)
                    edges.append(edge)

        for (fragment, fragment_id, _), (concept, concept_created) in zip(concept_upserts, store.batch_upsert_nodes([item[2] for item in concept_upserts])):
            _track_node(result, concept.id, concept_created)
            common = {
                "project_id": artifact.project_id,
                "artifact_id": artifact.id,
                "relative_path": artifact.relative_path,
                "fragment_id": fragment_id,
                "section_path": fragment.section_path,
            }
            edges.append(_typed_edge(artifact.id, concept.id, "CONTAINS", common, source_file=artifact.relative_path, line_start=fragment.start_line, line_end=fragment.end_line, extractor=parse_result.parser_name, evidence=fragment.text))
            edges.append(_typed_edge(fragment_id, concept.id, "CONTAINS", common, source_file=artifact.relative_path, line_start=fragment.start_line, line_end=fragment.end_line, extractor=parse_result.parser_name, evidence=fragment.text))
            edges.append(_typed_edge(concept.id, fragment_id, "DERIVED_FROM", common, source_file=artifact.relative_path, line_start=fragment.start_line, line_end=fragment.end_line, extractor=parse_result.parser_name, evidence=fragment.text))

        uri_upserts: list[tuple[dict[str, Any], str, str, MemoryNode]] = []
        for link in parse_result.links:
            raw_source_id = str(link.get("source_fragment_id") or "")
            source_id = fragment_id_map.get(raw_source_id, raw_source_id)
            uri = str(link.get("uri") or "")
            if not source_id or not uri:
                continue
            uri_upserts.append((link, source_id, uri, _uri_node(uri, str(link.get("text") or uri))))

        for (link, source_id, uri, _), (target, created) in zip(uri_upserts, store.batch_upsert_nodes([item[3] for item in uri_upserts])):
            _track_node(result, target.id, created)
            edge = _typed_edge(source_id, target.id, "LINKS_TO", {"project_id": artifact.project_id, "artifact_id": artifact.id, "relative_path": artifact.relative_path, "uri": uri}, source_file=artifact.relative_path, line_start=None, line_end=None, extractor=parse_result.parser_name, evidence=uri)
            edges.append(edge)

        _upsert_edges_deduped(store, result, edges)
        return result

    def _persist_compile_classification(self, store: GraphStore, artifact: SourceArtifact) -> ArtifactCompilationResult:
        result = ArtifactCompilationResult(artifact_id=artifact.id)
        file_id = _file_id(artifact)
        node_upserts: list[tuple[str, MemoryNode]] = []
        edges: list[MemoryEdge] = []
        if artifact.artifact_type == "config":
            node_upserts.append(("config", _config_node(artifact)))
        if _is_test_artifact(artifact):
            node_upserts.append(("test", _test_node(artifact)))
        for kind, (node, created) in zip([kind for kind, _ in node_upserts], store.batch_upsert_nodes([node for _, node in node_upserts])):
            _track_node(result, node.id, created)
            edges.append(
                _typed_edge(
                    file_id,
                    node.id,
                    "DEFINES",
                    {
                        "project_id": artifact.project_id,
                        "artifact_id": artifact.id,
                        "relative_path": artifact.relative_path,
                        "context_scope": artifact_context_scope(artifact),
                        "kind": kind,
                    },
                    source_file=artifact.relative_path,
                    line_start=1,
                    line_end=1,
                    extractor="project_scanner",
                    evidence=artifact.relative_path,
                )
            )
        _upsert_edges_deduped(store, result, edges)
        return result

    def _persist_document_processing(
        self,
        store: GraphStore,
        artifact: SourceArtifact,
        parse_result: DocumentParseResult,
        fragment_id_map: dict[str, str],
    ) -> ArtifactCompilationResult:
        result = ArtifactCompilationResult(artifact_id=artifact.id)
        fragments = [fragment for fragment in parse_result.fragments if str(fragment.text or "").strip()]
        if not fragments:
            return result
        output = self.document_processor.process(fragments)
        current_node_ids: set[str] = set()
        current_edge_ids: set[str] = set()
        concept_nodes = {term.key: _document_term_node(artifact, term, output) for term in output.terms}
        raw_event_nodes = {event.id_key: _document_raw_event_node(artifact, event) for event in output.raw_events}
        for node, created in store.batch_upsert_nodes([*concept_nodes.values(), *raw_event_nodes.values()]):
            _track_node(result, node.id, created)
            current_node_ids.add(node.id)
        edges: list[MemoryEdge] = []
        for term in output.terms:
            concept = concept_nodes.get(term.key)
            if concept is None:
                continue
            edges.append(_typed_edge(artifact.id, concept.id, "CONTAINS", _document_processing_edge_properties(artifact, evidence=term.evidence, rank=term.rank), source_file=artifact.relative_path, line_start=None, line_end=None, extractor="document_processor", evidence=term.evidence, is_semantic=True, is_technical=False))
            for event in term.raw_events:
                event_node = raw_event_nodes.get(event.id_key)
                if event_node is None:
                    continue
                fragment_id = fragment_id_map.get(event.fragment_id, event.fragment_id)
                props = _document_processing_edge_properties(artifact, evidence=event.evidence, rank=event.rank)
                edges.append(_typed_edge(fragment_id, concept.id, "MENTIONS", props, source_file=artifact.relative_path, line_start=event.start_line, line_end=event.end_line, extractor="document_processor", evidence=event.evidence, is_semantic=True, is_technical=False))
                edges.append(_typed_edge(concept.id, event_node.id, "EVIDENCED_BY", props, source_file=artifact.relative_path, line_start=event.start_line, line_end=event.end_line, extractor="document_processor", evidence=event.evidence, is_semantic=True, is_technical=False))
                edges.append(_typed_edge(event_node.id, fragment_id, "DERIVED_FROM", props, source_file=artifact.relative_path, line_start=event.start_line, line_end=event.end_line, extractor="document_processor", evidence=event.evidence, is_semantic=True, is_technical=False))
        for relation in output.relations:
            subject = concept_nodes.get(relation.source_key)
            target = concept_nodes.get(relation.target_key)
            if subject is None or target is None or subject.id == target.id:
                continue
            props = _document_processing_edge_properties(artifact, evidence=relation.evidence, rank=relation.rank)
            props.update({"predicate": relation.relation, "cooccurrence_count": relation.cooccurrence_count, "fragment_count": relation.fragment_count, "evidence_fragment_id": fragment_id_map.get(relation.evidence_fragment_id, relation.evidence_fragment_id)})
            edges.append(_typed_edge(subject.id, target.id, "CO_OCCURS_WITH", props, source_file=artifact.relative_path, line_start=None, line_end=None, extractor="document_processor", evidence=relation.evidence, is_semantic=True, is_technical=False))
        _upsert_edges_deduped(store, result, edges)
        current_edge_ids.update(result.added_edges)
        current_edge_ids.update(result.updated_edges)
        self._archive_stale_document_processing(store, artifact, result, current_node_ids=current_node_ids, current_edge_ids=current_edge_ids)
        return result

    def _archive_stale_document_processing(
        self,
        store: GraphStore,
        artifact: SourceArtifact,
        result: ArtifactCompilationResult,
        *,
        current_node_ids: set[str],
        current_edge_ids: set[str],
    ) -> None:
        for edge in store.find_edges_by_property("artifact_id", artifact.id, limit=100000):
            if edge.properties.get("extractor") != "document_processor" or edge.id in current_edge_ids or edge.properties.get("status") == "archived":
                continue
            _archive_edge(store, edge)
            result.archived_edges.append(edge.id)
            result.affected_edge_ids.add(edge.id)
        for node_type in ("Concept", "RawEvent"):
            for node in store.find_nodes_by_property("artifact_id", artifact.id, type_=node_type, limit=100000):
                if node.properties.get("extractor") != "document_processor" or node.id in current_node_ids or node.status == "archived":
                    continue
                _archive_concept(store, node)
                result.archived_nodes.append(node.id)
                result.affected_node_ids.add(node.id)

    def _persist_code_graph(
        self,
        store: GraphStore,
        artifact: SourceArtifact,
        code_result: CodeParseResult,
        parse_result: DocumentParseResult,
    ) -> ArtifactCompilationResult:
        result = ArtifactCompilationResult(artifact_id=artifact.id)
        current_ids: set[str] = set()
        fragment_by_symbol = {
            str(fragment.metadata.get("symbol_qualified_name")): fragment.id
            for fragment in parse_result.fragments
            if fragment.metadata.get("symbol_qualified_name")
        }
        file_id = _file_id(artifact)
        pending_edges: list[MemoryEdge] = []
        external_nodes: dict[tuple[str, str], MemoryNode] = {}
        import_node_ids: dict[str, str] = {}
        imported_names = _imported_binding_names(code_result.imports)

        def upsert_external(name: str | None, kind: str) -> MemoryNode | None:
            clean_name = _clean_external_symbol_name(name, imported_names=imported_names)
            if clean_name is None:
                return None
            key = (clean_name, kind)
            cached = external_nodes.get(key)
            if cached is not None:
                return cached
            target, created = store.upsert_node(_external_code_symbol_node(artifact, clean_name, kind))
            current_ids.add(target.id)
            _track_node(result, target.id, created)
            external_nodes[key] = target
            return target

        module_node, created = store.upsert_node(_module_node(artifact, code_result.module, code_result))
        current_ids.add(module_node.id)
        _track_node(result, module_node.id, created)
        for edge in (
            _typed_edge(
                artifact.id,
                module_node.id,
                "DEFINES",
                {"project_id": artifact.project_id, "artifact_id": artifact.id, "kind": "module"},
                source_file=artifact.relative_path,
                line_start=1,
                line_end=1,
                extractor=code_result.parser_name,
                evidence=code_result.module.name,
            ),
            _typed_edge(
                file_id,
                module_node.id,
                "DEFINES",
                {"project_id": artifact.project_id, "artifact_id": artifact.id, "kind": "module"},
                source_file=artifact.relative_path,
                line_start=1,
                line_end=1,
                extractor=code_result.parser_name,
                evidence=code_result.module.name,
            ),
        ):
            pending_edges.append(edge)

        read_references = [reference for reference in code_result.references if reference.access in {"read", "return", "raise"}]
        symbol_nodes: dict[str, MemoryNode] = {}
        skipped_local_variables = {
            symbol.qualified_name
            for symbol in code_result.symbols
            if symbol.kind == "variable" and not _should_persist_variable(symbol, read_references)
        }
        symbol_upserts = [
            (symbol, _symbol_node(artifact, symbol, code_result))
            for symbol in code_result.symbols
            if symbol.qualified_name not in skipped_local_variables
        ]
        for (symbol, _), (node, node_created) in zip(symbol_upserts, store.batch_upsert_nodes([node for _, node in symbol_upserts])):
            current_ids.add(node.id)
            symbol_nodes[symbol.qualified_name] = node
            _track_node(result, node.id, node_created)
        symbol_resolver = _SymbolNodeResolver(symbol_nodes)

        for symbol in code_result.symbols:
            node = symbol_nodes.get(symbol.qualified_name)
            if node is None:
                continue
            for edge in _symbol_edges(artifact, file_id, module_node.id, symbol, node.id, symbol_nodes, code_result.parser_name):
                pending_edges.append(edge)
            fragment_id = fragment_by_symbol.get(symbol.qualified_name)
            if fragment_id:
                pending_edges.append(
                    _typed_edge(
                        node.id,
                        fragment_id,
                        "EVIDENCED_BY",
                        {
                            "project_id": artifact.project_id,
                            "artifact_id": artifact.id,
                            "relative_path": artifact.relative_path,
                            "symbol_name": symbol.name,
                            "symbol_kind": symbol.kind,
                            "symbol_qualified_name": symbol.qualified_name,
                            "fragment_id": fragment_id,
                        },
                        source_file=artifact.relative_path,
                        line_start=symbol.start_line,
                        line_end=symbol.end_line,
                        extractor=code_result.parser_name,
                        evidence=symbol.qualified_name,
                    )
                )
        schema_upserts: list[tuple[CodeSymbol, MemoryNode, str]] = []
        for symbol in code_result.symbols:
            node = symbol_nodes.get(symbol.qualified_name)
            if node is None:
                continue
            if symbol.metadata.get("is_schema"):
                schema_upserts.append((symbol, _schema_node(artifact, symbol), node.id))
        for (symbol, _, parent_node_id), (schema_node, schema_created) in zip(schema_upserts, store.batch_upsert_nodes([node for _, node, _ in schema_upserts])):
            current_ids.add(schema_node.id)
            _track_node(result, schema_node.id, schema_created)
            pending_edges.append(
                _typed_edge(
                    parent_node_id,
                    schema_node.id,
                    "DEFINES",
                    {"project_id": artifact.project_id, "artifact_id": artifact.id, "schema": symbol.name},
                    source_file=artifact.relative_path,
                    line_start=symbol.start_line,
                    line_end=symbol.end_line,
                    extractor=code_result.parser_name,
                    evidence=symbol.name,
                )
            )

        endpoint_upserts: list[tuple[CodeSymbol, str, str, MemoryNode]] = []
        for symbol in code_result.symbols:
            node = symbol_nodes.get(symbol.qualified_name)
            if not node:
                continue
            for decorator in symbol.decorators:
                target = upsert_external(decorator, "decorator")
                if target is None:
                    continue
                pending_edges.append(_typed_edge(node.id, target.id, "DECORATED_BY", {"project_id": artifact.project_id, "artifact_id": artifact.id, "decorator": decorator}, source_file=artifact.relative_path, line_start=symbol.start_line, line_end=symbol.start_line, extractor=code_result.parser_name, evidence=decorator))
                endpoint_node = _endpoint_node_for_decorator(artifact, symbol, decorator)
                if endpoint_node is not None:
                    endpoint_upserts.append((symbol, node.id, decorator, endpoint_node))
            for base in symbol.bases:
                target = symbol_resolver.resolve(base) or upsert_external(base, "external")
                if target is None:
                    continue
                current_ids.add(target.id)
                relation_type = "IMPLEMENTS" if node.type == "Interface" or symbol_resolver.is_interface(base) else "INHERITS"
                pending_edges.append(_typed_edge(node.id, target.id, relation_type, {"project_id": artifact.project_id, "artifact_id": artifact.id, "base": base}, source_file=artifact.relative_path, line_start=symbol.start_line, line_end=symbol.start_line, extractor=code_result.parser_name, evidence=base))
            target = upsert_external(symbol.returns, "external")
            if target is not None:
                pending_edges.append(_typed_edge(node.id, target.id, "RETURNS", {"project_id": artifact.project_id, "artifact_id": artifact.id, "hint": symbol.returns}, source_file=artifact.relative_path, line_start=symbol.start_line, line_end=symbol.start_line, extractor=code_result.parser_name, evidence=symbol.returns or ""))
        for (symbol, handler_node_id, decorator, _), (endpoint, endpoint_created) in zip(endpoint_upserts, store.batch_upsert_nodes([node for _, _, _, node in endpoint_upserts])):
            current_ids.add(endpoint.id)
            _track_node(result, endpoint.id, endpoint_created)
            pending_edges.append(
                _typed_edge(
                    handler_node_id,
                    endpoint.id,
                    "HANDLES_ROUTE",
                    {"project_id": artifact.project_id, "artifact_id": artifact.id, "route": endpoint.properties.get("path"), "handler": symbol.qualified_name},
                    source_file=artifact.relative_path,
                    line_start=symbol.start_line,
                    line_end=symbol.start_line,
                    extractor=code_result.parser_name,
                    evidence=decorator,
                )
            )

        import_nodes = [(item, _import_node(artifact, item)) for item in code_result.imports]
        dependency_nodes = [(item, _dependency_node(artifact, item)) for item in code_result.imports]
        for (item, _), (node, node_created) in zip(import_nodes, store.batch_upsert_nodes([node for _, node in import_nodes])):
            current_ids.add(node.id)
            import_node_ids[item.id] = node.id
            _track_node(result, node.id, node_created)
            pending_edges.append(_typed_edge(module_node.id, node.id, "IMPORTS", {"project_id": artifact.project_id, "artifact_id": artifact.id, "module": item.module, "name": item.name}, source_file=artifact.relative_path, line_start=item.line, line_end=item.line, extractor=code_result.parser_name, evidence=item.raw or item.module or item.name or ""))
            pending_edges.append(_typed_edge(file_id, node.id, "IMPORTS", {"project_id": artifact.project_id, "artifact_id": artifact.id, "module": item.module, "name": item.name}, source_file=artifact.relative_path, line_start=item.line, line_end=item.line, extractor=code_result.parser_name, evidence=item.raw or item.module or item.name or ""))
            if _is_package_init_artifact(artifact):
                pending_edges.append(_typed_edge(module_node.id, node.id, "RE_EXPORTS", {"project_id": artifact.project_id, "artifact_id": artifact.id, "module": item.module, "name": item.name, "alias": item.alias}, source_file=artifact.relative_path, line_start=item.line, line_end=item.line, extractor=code_result.parser_name, evidence=item.raw or item.module or item.name or ""))
        for (item, _), (dependency, dep_created) in zip(dependency_nodes, store.batch_upsert_nodes([node for _, node in dependency_nodes])):
            current_ids.add(dependency.id)
            _track_node(result, dependency.id, dep_created)
            pending_edges.append(_typed_edge(file_id, dependency.id, "DEPENDS_ON", {"project_id": artifact.project_id, "artifact_id": artifact.id, "module": item.module, "name": item.name}, source_file=artifact.relative_path, line_start=item.line, line_end=item.line, extractor=code_result.parser_name, evidence=item.raw or item.module or item.name or ""))
            pending_edges.append(_typed_edge(module_node.id, dependency.id, "IMPORTS_FROM", {"project_id": artifact.project_id, "artifact_id": artifact.id, "module": item.module, "name": item.name}, source_file=artifact.relative_path, line_start=item.line, line_end=item.line, extractor=code_result.parser_name, evidence=item.raw or item.module or item.name or ""))

        table = SymbolTable(code_result)
        unresolved_calls_by_owner: dict[str, list[dict[str, Any]]] = {}
        for call in code_result.calls:
            caller_node = symbol_nodes.get(call.caller or "")
            resolved = table.resolve_call_target(call.target, caller=call.caller)
            target_node = symbol_nodes.get(resolved.qualified_name) if resolved else None
            if caller_node is None:
                continue
            if target_node is not None:
                relation_type = "INSTANTIATES" if target_node.type in {"Class", "Interface"} else "CALLS"
                pending_edges.append(_typed_edge(caller_node.id, target_node.id, relation_type, {"project_id": artifact.project_id, "artifact_id": artifact.id, "target": call.target, "line": call.line}, source_file=artifact.relative_path, line_start=call.line, line_end=call.line, extractor=code_result.parser_name, evidence=call.target))
                continue
            if _should_record_unresolved_call(call.target, imported_names=imported_names):
                unresolved_calls_by_owner.setdefault(caller_node.id, []).append(
                    {
                        "target": call.target,
                        "line": call.line,
                        "column": call.column,
                        "reason": "unresolved_static_call",
                    }
                )
        _store_unresolved_call_summaries(store, unresolved_calls_by_owner)

        for reference in code_result.references:
            owner = symbol_nodes.get(reference.owner or "") or module_node
            target_symbol = table.resolve_call_target(reference.name, caller=reference.owner)
            target_node = symbol_nodes.get(target_symbol.qualified_name) if target_symbol else None
            if target_node is None and reference.access in {"raise", "return"}:
                if _reference_targets_local_argument(reference, symbol_nodes):
                    continue
                target_node = upsert_external(reference.name, "external")
            if target_node is None or owner.id == target_node.id:
                continue
            relation = {"read": "READS", "write": "WRITES", "raise": "RAISES", "return": "RETURNS"}.get(str(reference.access or ""), "REFERENCES")
            for relation_type in (relation,):
                pending_edges.append(
                    _typed_edge(
                        owner.id,
                        target_node.id,
                        relation_type,
                        {"project_id": artifact.project_id, "artifact_id": artifact.id, "name": reference.name, "access": reference.access},
                        source_file=artifact.relative_path,
                        line_start=reference.line,
                        line_end=reference.line,
                        extractor=code_result.parser_name,
                        evidence=reference.name,
                    )
                )

        comment_nodes = [(text_item, _code_text_node(artifact, text_item)) for text_item in code_result.comments]
        for (text_item, _), (node, node_created) in zip(comment_nodes, store.batch_upsert_nodes([node for _, node in comment_nodes])):
            current_ids.add(node.id)
            _track_node(result, node.id, node_created)
            owner = symbol_nodes.get(text_item.owner or "") or module_node
            pending_edges.append(_typed_edge(owner.id, node.id, "HAS_COMMENT", {"project_id": artifact.project_id, "artifact_id": artifact.id}, source_file=artifact.relative_path, line_start=text_item.start_line, line_end=text_item.end_line, extractor=code_result.parser_name, evidence=(text_item.text or "")[:200]))

        docstring_nodes = [(text_item, _code_text_node(artifact, text_item)) for text_item in code_result.docstrings]
        for (text_item, _), (node, node_created) in zip(docstring_nodes, store.batch_upsert_nodes([node for _, node in docstring_nodes])):
            current_ids.add(node.id)
            _track_node(result, node.id, node_created)
            owner = symbol_nodes.get(text_item.owner or "") or module_node
            pending_edges.append(_typed_edge(owner.id, node.id, "HAS_DOCSTRING", {"project_id": artifact.project_id, "artifact_id": artifact.id}, source_file=artifact.relative_path, line_start=text_item.start_line, line_end=text_item.end_line, extractor=code_result.parser_name, evidence=(text_item.text or "")[:200]))

        finding_upserts = _static_analysis_finding_nodes(
            artifact,
            code_result,
            symbol_nodes,
            import_node_ids,
            table,
        )
        for (finding, target_node_id), (node, node_created) in zip(
            finding_upserts,
            store.batch_upsert_nodes([item[0] for item in finding_upserts]),
        ):
            current_ids.add(node.id)
            _track_node(result, node.id, node_created)
            pending_edges.append(
                _typed_edge(
                    artifact.id,
                    node.id,
                    "HAS_FINDING",
                    {
                        "project_id": artifact.project_id,
                        "artifact_id": artifact.id,
                        "relative_path": artifact.relative_path,
                        "finding_type": node.properties.get("finding_type"),
                    },
                    source_file=artifact.relative_path,
                    line_start=node.properties.get("line_start"),
                    line_end=node.properties.get("line_end"),
                    extractor=code_result.parser_name,
                    evidence=str(node.properties.get("reason") or node.label or ""),
                )
            )
            pending_edges.append(
                _typed_edge(
                    target_node_id,
                    node.id,
                    "HAS_FINDING",
                    {
                        "project_id": artifact.project_id,
                        "artifact_id": artifact.id,
                        "relative_path": artifact.relative_path,
                        "finding_type": node.properties.get("finding_type"),
                    },
                    source_file=artifact.relative_path,
                    line_start=node.properties.get("line_start"),
                    line_end=node.properties.get("line_end"),
                    extractor=code_result.parser_name,
                    evidence=str(node.properties.get("reason") or node.label or ""),
                )
            )

        _upsert_edges_deduped(store, result, pending_edges)

        stale_nodes = [old_node for old_node in _code_nodes_for_artifact(store, artifact) if old_node.id not in current_ids and old_node.status != "archived"]
        stale_edges = _code_related_edges_for_nodes(store, stale_nodes)
        for old_node in stale_nodes:
            _archive_code_node(store, old_node)
            result.archived_nodes.append(old_node.id)
            result.affected_node_ids.add(old_node.id)
        for edge in stale_edges:
            _archive_edge(store, edge)
            result.archived_edges.append(edge.id)
            result.affected_edge_ids.add(edge.id)

        return result


def archive_artifact_fragments(store: GraphStore, artifact: SourceArtifact | MemoryNode) -> ArtifactCompilationResult:
    artifact_id = artifact.id
    project_id = artifact.project_id if isinstance(artifact, SourceArtifact) else str(artifact.properties.get("project_id"))
    result = ArtifactCompilationResult(artifact_id=artifact_id)
    for node in _fragments_for_artifact_id(store, project_id, artifact_id):
        if node.status != "archived":
            _archive_fragment(store, node)
            result.archived_nodes.append(node.id)
        result.affected_node_ids.add(node.id)
        for edge in _fragment_related_edges(store, node):
            _archive_edge(store, edge)
            result.archived_edges.append(edge.id)
            result.affected_edge_ids.add(edge.id)
    code_nodes = [node for node in _code_nodes_for_artifact(store, artifact) if node.status != "archived"]
    code_edges = _code_related_edges_for_nodes(store, code_nodes)
    for node in code_nodes:
        _archive_code_node(store, node)
        result.archived_nodes.append(node.id)
        result.affected_node_ids.add(node.id)
    for edge in code_edges:
        _archive_edge(store, edge)
        result.archived_edges.append(edge.id)
        result.affected_edge_ids.add(edge.id)
    for node_type in ("Concept", "RawEvent"):
        for node in store.find_nodes_by_property("artifact_id", artifact_id, type_=node_type, limit=100000):
            if node.status == "archived" or node.properties.get("extractor") != "document_processor":
                continue
            _archive_concept(store, node)
            result.archived_nodes.append(node.id)
            result.affected_node_ids.add(node.id)
            for edge in store.incident_edges([node.id], limit=10000):
                if edge.properties.get("artifact_id") != artifact_id or edge.properties.get("status") == "archived":
                    continue
                _archive_edge(store, edge)
                result.archived_edges.append(edge.id)
                result.affected_edge_ids.add(edge.id)
    return result


def link_document_fragments_to_code(
    store: GraphStore,
    *,

    project_id: str,
    artifact_ids: set[str] | None = None,
) -> ArtifactCompilationResult:
    """Link compiled documentation fragments to code symbols they explicitly mention."""

    result = ArtifactCompilationResult(artifact_id="*")
    fragments = _semantic_document_fragments(store, project_id, artifact_ids)
    if not fragments:
        return result
    code_nodes = _project_code_nodes(store, project_id)
    if not code_nodes:
        return result

    pending_edges: list[MemoryEdge] = []
    current_targets_by_fragment: dict[str, set[str]] = {}
    current_term_code_pairs: set[tuple[str, str]] = set()
    has_existing_document_code_links = any(
        edge.properties.get("extractor") in {"document_code_linker", "document_processor"}
        for edge in store.find_edges_by_property("project_id", project_id, type_="REFERENCES", limit=100000)
    )
    simple_index, complex_matchers = _document_code_link_index(code_nodes)
    link_limit_reached = False
    for fragment in fragments:
        if len(pending_edges) >= MAX_DOCUMENT_CODE_LINKS_PER_RUN:
            link_limit_reached = True
            break
        text = str(fragment.text or "")
        if not text.strip():
            continue
        matched_targets: set[str] = set()
        for code_node, evidence in _matching_code_targets(text, simple_index, complex_matchers):
            if len(matched_targets) >= MAX_DOCUMENT_CODE_LINKS_PER_FRAGMENT or len(pending_edges) >= MAX_DOCUMENT_CODE_LINKS_PER_RUN:
                link_limit_reached = len(pending_edges) >= MAX_DOCUMENT_CODE_LINKS_PER_RUN
                break
            if code_node.id in matched_targets:
                continue
            matched_targets.add(code_node.id)
            pending_edges.append(
                _typed_edge(
                    fragment.id,
                    code_node.id,
                    "REFERENCES",
                    {
                        "project_id": project_id,
                        "artifact_id": fragment.properties.get("artifact_id"),
                        "relative_path": fragment.properties.get("relative_path"),
                        "target_name": code_node.properties.get("name") or code_node.label,
                        "target_type": code_node.type,
                        "target_artifact_id": code_node.properties.get("artifact_id"),
                    },
                    source_file=str(fragment.properties.get("relative_path") or ""),
                    line_start=_int_or_none(fragment.properties.get("start_line")),
                    line_end=_int_or_none(fragment.properties.get("end_line")),
                    extractor="document_code_linker",
                    evidence=evidence,
                    is_semantic=False,
                    is_technical=True,
                )
            )
            for term_node in _document_processor_terms_for_fragment(store, fragment):
                current_term_code_pairs.add((term_node.id, code_node.id))
                pending_edges.append(
                    _typed_edge(
                        term_node.id,
                        code_node.id,
                        "REFERENCES",
                        {
                            "project_id": project_id,
                            "artifact_id": fragment.properties.get("artifact_id"),
                            "relative_path": fragment.properties.get("relative_path"),
                            "target_name": code_node.properties.get("name") or code_node.label,
                            "target_type": code_node.type,
                            "target_artifact_id": code_node.properties.get("artifact_id"),
                            "fragment_id": fragment.id,
                            "rank": term_node.properties.get("rank", 0.0),
                        },
                        source_file=str(fragment.properties.get("relative_path") or ""),
                        line_start=_int_or_none(fragment.properties.get("start_line")),
                        line_end=_int_or_none(fragment.properties.get("end_line")),
                        extractor="document_processor",
                        evidence=evidence,
                        is_semantic=True,
                        is_technical=True,
                    )
                )
        current_targets_by_fragment[fragment.id] = matched_targets

    _upsert_edges_deduped(store, result, pending_edges)
    if has_existing_document_code_links and not link_limit_reached:
        _archive_stale_document_code_links(store, result, project_id=project_id, current_targets_by_fragment=current_targets_by_fragment)
        _archive_stale_document_processor_code_links(store, result, project_id=project_id, current_pairs=current_term_code_pairs)
    return result


def _fragment_node(artifact: SourceArtifact, fragment: DocumentFragment, parse_result: DocumentParseResult) -> MemoryNode:
    now = utcnow_iso()
    properties = fragment.to_dict()
    properties.update(fragment.metadata)
    properties.update(
        {
            "project_id": artifact.project_id,
            "artifact_id": artifact.id,
            "relative_path": artifact.relative_path,
            "context_scope": artifact_context_scope(artifact),
            "artifact_sha256": artifact.sha256,
            "artifact_type": artifact.artifact_type,
            "language": artifact.language,
            "parser_name": parse_result.parser_name,
            "parser_version": parse_result.parser_version,
            "parser_errors": list(parse_result.errors),
            "document_title": parse_result.title,
            "status": "active",
            "updated_at": now,
        }
    )
    label = f"{artifact.relative_path}#{properties.get('fragment_index', fragment.id)}"
    return MemoryNode(
        id=fragment.id,

        type="SourceFragment",
        label=label,
        text=fragment.text,
        canonical_key=f"{artifact.id}:{fragment.metadata.get('structural_hash', fragment.id)}",
        properties=properties,
        salience=0.12,
        confidence=fragment.confidence,
        status="active",
    )


def _fragment_edges(artifact: SourceArtifact, fragment: DocumentFragment, fragment_node_id: str) -> list[MemoryEdge]:
    edges = [
        _typed_edge(
            artifact.id,
            fragment_node_id,
            "CONTAINS_FRAGMENT",
            {
                "project_id": artifact.project_id,
                "artifact_id": artifact.id,
                "relative_path": artifact.relative_path,
                "context_scope": artifact_context_scope(artifact),
                "fragment_type": fragment.fragment_type,
                "status": "active",
            },
            source_file=artifact.relative_path,
            line_start=fragment.start_line,
            line_end=fragment.end_line,
            extractor="document_parser",
            evidence=fragment.fragment_type,
        ),
        _typed_edge(
            fragment_node_id,
            artifact.id,
            "DERIVED_FROM",
            {
                "project_id": artifact.project_id,
                "artifact_id": artifact.id,
                "relative_path": artifact.relative_path,
                "context_scope": artifact_context_scope(artifact),
                "fragment_type": fragment.fragment_type,
                "status": "active",
            },
            source_file=artifact.relative_path,
            line_start=fragment.start_line,
            line_end=fragment.end_line,
            extractor="document_parser",
            evidence=fragment.fragment_type,
        ),
    ]
    if fragment.fragment_type == "heading":
        edges.append(
            _typed_edge(
                artifact.id,
                fragment_node_id,
                "HAS_SECTION",
                {
                    "project_id": artifact.project_id,
                    "artifact_id": artifact.id,
                    "relative_path": artifact.relative_path,
                    "context_scope": artifact_context_scope(artifact),
                    "section_path": fragment.section_path,
                    "status": "active",
                },
                source_file=artifact.relative_path,
                line_start=fragment.start_line,
                line_end=fragment.end_line,
                extractor="document_parser",
                evidence=fragment.section_path,
            )
        )
    return edges


def _artifact_fragments(store: GraphStore, artifact: SourceArtifact) -> list[MemoryNode]:
    return _fragments_for_artifact_id(store, artifact.project_id, artifact.id)


def _fragments_for_artifact_id(store: GraphStore, project_id: str, artifact_id: str) -> list[MemoryNode]:
    return [
        node
        for node in store.find_nodes_by_property("artifact_id", artifact_id, type_="SourceFragment", limit=100000)
        if node.type == "SourceFragment"
        and node.properties.get("project_id") == project_id
        and node.properties.get("artifact_id") == artifact_id
    ]


def _fragment_related_edges(store: GraphStore, node: MemoryNode) -> list[MemoryEdge]:
    edge_types = [
        "DERIVED_FROM",
        "CONTAINS_FRAGMENT",
        "CONTAINS",
        "HAS_SECTION",
        "LINKS_TO",
        "HAS_CODE_BLOCK",
        "REFERENCES",
        "MENTIONS",
    ]
    edges: list[MemoryEdge] = []
    for edge_type in edge_types:
        edges.extend(store.get_edges(from_id=node.id, type_=edge_type, limit=100))
        edges.extend(store.get_edges(to_id=node.id, type_=edge_type, limit=100))
    deduped: dict[str, MemoryEdge] = {}
    for edge in edges:
        deduped[edge.id] = edge
    return list(deduped.values())


def _concepts_for_fragment(store: GraphStore, fragment: MemoryNode) -> list[MemoryNode]:
    concepts: dict[str, MemoryNode] = {}
    for edge in store.get_edges(from_id=fragment.id, type_="CONTAINS", limit=100):
        node = store.get_node(edge.to_id)
        if node is not None and node.type == "Concept":
            concepts[node.id] = node
    return list(concepts.values())


def _document_processor_terms_for_fragment(store: GraphStore, fragment: MemoryNode) -> list[MemoryNode]:
    terms: dict[str, MemoryNode] = {}
    for edge in store.get_edges(from_id=fragment.id, type_="MENTIONS", limit=100):
        if edge.properties.get("extractor") != "document_processor":
            continue
        node = store.get_node(edge.to_id)
        if node is not None and node.type == "Concept" and node.status != "archived":
            terms[node.id] = node
    return sorted(terms.values(), key=lambda node: float(node.properties.get("rank") or 0.0), reverse=True)[:8]


def _semantic_document_fragments(
    store: GraphStore,

    project_id: str,
    artifact_ids: set[str] | None,
) -> list[MemoryNode]:
    fragments: list[MemoryNode] = []
    for node in store.find_nodes_by_property("project_id", project_id, type_="SourceFragment", limit=100000):
        if node.status == "archived":
            continue
        if node.properties.get("project_id") != project_id:
            continue
        if artifact_ids is not None and str(node.properties.get("artifact_id") or "") not in artifact_ids:
            continue
        if str(node.properties.get("artifact_type") or "") not in {"markdown", "text", "config", "data", "unknown"}:
            continue
        if not _source_fragment_node_should_link_to_code(node):
            continue
        fragments.append(node)
    return fragments


def _project_code_nodes(store: GraphStore, project_id: str) -> list[MemoryNode]:
    nodes: list[MemoryNode] = []
    for node in store.find_nodes_by_property("project_id", project_id, limit=100000):
        if node.status != "archived" and node.type in DOCUMENT_CODE_LINK_NODE_TYPES and node.properties.get("project_id") == project_id:
            nodes.append(node)
    nodes.sort(key=lambda item: (item.type, item.label.casefold(), item.id))
    return nodes


def _source_fragment_node_should_link_to_code(node: MemoryNode) -> bool:
    fragment_type = str(node.properties.get("fragment_type") or "")
    text = str(node.text or "")
    stripped = text.strip()
    if fragment_type in {"code_block", "metadata"} or len(stripped) < 3:
        return False
    if fragment_type == "heading":
        heading_key = re.sub(r"[^A-Za-z0-9_]+", " ", stripped).strip().casefold()
        if heading_key in DOCUMENT_CODE_LINK_STOP_TERMS or len(heading_key.split()) <= 2 and any(token in DOCUMENT_CODE_LINK_STOP_TERMS for token in heading_key.split()):
            return False
    return True


def _code_node_terms(node: MemoryNode) -> list[str]:
    raw_terms = [
        node.label,
        node.properties.get("name"),
        node.properties.get("qualified_name"),
        node.properties.get("handler"),
        node.properties.get("route"),
        node.properties.get("path"),
    ]
    terms: set[str] = set()
    for raw in raw_terms:
        if not raw:
            continue
        value = str(raw).strip()
        if not _useful_code_link_term(value, node):
            continue
        terms.add(value)
        tail = value.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[-1]
        if _useful_code_link_term(tail, node) and _qualified_tail_is_safe(value, tail):
            terms.add(tail)
        qualified_tail = value.rsplit(".", 1)[-1]
        if _useful_code_link_term(qualified_tail, node) and _qualified_tail_is_safe(value, qualified_tail):
            terms.add(qualified_tail)
    return sorted(terms, key=lambda item: (-len(item), item.casefold()))


def _useful_code_link_term(value: str, node: MemoryNode) -> bool:
    stripped = value.strip()
    if len(stripped) < 3:
        return False
    if stripped.casefold() in DOCUMENT_CODE_LINK_STOP_TERMS:
        return False
    if len(stripped) < 5 and not any(separator in stripped for separator in ("/", "\\", ".", "_", "-")):
        return False
    if node.type in {"Module", "Function", "Method", "Class", "Interface"} and stripped == str(node.properties.get("name") or ""):
        if len(stripped) < 6 or stripped.casefold() in DOCUMENT_CODE_LINK_STOP_TERMS:
            return False
    return any(char.isalpha() for char in stripped)


def _qualified_tail_is_safe(value: str, tail: str) -> bool:
    if tail == value:
        return True
    if len(tail) >= 8:
        return True
    return "_" in tail or any(char.isupper() for char in tail[1:])


def _document_code_link_index(
    code_nodes: list[MemoryNode],
) -> tuple[dict[str, list[tuple[tuple[int, str, str, str, str], MemoryNode, str]]], list[tuple[tuple[int, str, str, str, str], MemoryNode, str]]]:
    simple_index: dict[str, list[tuple[tuple[int, str, str, str, str], MemoryNode, str]]] = {}
    complex_matchers: list[tuple[tuple[int, str, str, str, str], MemoryNode, str]] = []
    for node in code_nodes:
        for term in _code_node_terms(node):
            priority = (-len(term), node.type, node.label.casefold(), node.id, term.casefold())
            entry = (priority, node, term)
            if _term_requires_regex(term):
                complex_matchers.append(entry)
            else:
                simple_index.setdefault(term.casefold(), []).append(entry)
    for entries in simple_index.values():
        entries.sort(key=lambda item: item[0])
    complex_matchers.sort(key=lambda item: item[0])
    return simple_index, complex_matchers


def _matching_code_targets(
    text: str,
    simple_index: dict[str, list[tuple[tuple[int, str, str, str, str], MemoryNode, str]]],
    complex_matchers: list[tuple[tuple[int, str, str, str, str], MemoryNode, str]],
) -> list[tuple[MemoryNode, str]]:
    candidates: list[tuple[tuple[int, str, str, str, str], MemoryNode, str]] = []
    for token in _document_link_tokens(text):
        candidates.extend(simple_index.get(token, []))
    for entry in complex_matchers:
        _, _, term = entry
        if _text_mentions_term(text, term):
            candidates.append(entry)
    candidates.sort(key=lambda item: item[0])
    return [(node, term) for _, node, term in candidates]


def _document_link_tokens(text: str) -> set[str]:
    return {match.group(0).casefold() for match in re.finditer(r"(?<![A-Za-z0-9_])[A-Za-z_][A-Za-z0-9_]{2,}(?![A-Za-z0-9_])", text)}


def _term_requires_regex(term: str) -> bool:
    return any(separator in term for separator in ("/", "\\", ".", "-"))


def _text_mentions_term(text: str, term: str) -> bool:
    return bool(_term_pattern(term).search(text))


@lru_cache(maxsize=32768)
def _term_pattern(term: str) -> re.Pattern[str]:
    escaped = re.escape(term.strip())
    if not escaped:
        return re.compile(r"a\A")
    if any(separator in term for separator in ("/", "\\", ".", "_", "-")):
        return re.compile(rf"(?<![A-Za-z0-9_]){escaped}(?![A-Za-z0-9_])", flags=re.IGNORECASE)
    return re.compile(rf"\b{escaped}\b", flags=re.IGNORECASE)


def _archive_stale_document_code_links(
    store: GraphStore,
    result: ArtifactCompilationResult,
    *,

    project_id: str,
    current_targets_by_fragment: dict[str, set[str]],
) -> None:
    current_pairs = {
        (fragment_id, target_id)
        for fragment_id, target_ids in current_targets_by_fragment.items()
        for target_id in target_ids
    }
    existing = store.find_edges_by_property("project_id", project_id, type_="REFERENCES", limit=100000)
    for edge in existing:
        if edge.properties.get("extractor") != "document_code_linker":
            continue
        if (edge.from_id, edge.to_id) in current_pairs or edge.properties.get("status") == "archived":
            continue
        _archive_edge(store, edge)
        result.archived_edges.append(edge.id)
        result.affected_edge_ids.add(edge.id)


def _archive_stale_document_processor_code_links(
    store: GraphStore,
    result: ArtifactCompilationResult,
    *,
    project_id: str,
    current_pairs: set[tuple[str, str]],
) -> None:
    existing = store.find_edges_by_property("project_id", project_id, type_="REFERENCES", limit=100000)
    for edge in existing:
        if edge.properties.get("extractor") != "document_processor":
            continue
        if edge.properties.get("source_layer") != "project_document" or not edge.properties.get("is_technical"):
            continue
        if (edge.from_id, edge.to_id) in current_pairs or edge.properties.get("status") == "archived":
            continue
        _archive_edge(store, edge)
        result.archived_edges.append(edge.id)
        result.affected_edge_ids.add(edge.id)


def _int_or_none(value: object) -> int | None:
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _archive_fragment(store: GraphStore, node: MemoryNode) -> None:
    properties = dict(node.properties)
    properties["status"] = "archived"
    properties["updated_at"] = utcnow_iso()
    store.update_node_fields(node.id, status="archived", properties=properties)


def _archive_concept(store: GraphStore, node: MemoryNode) -> None:
    properties = dict(node.properties)
    properties["status"] = "archived"
    properties["updated_at"] = utcnow_iso()
    store.update_node_fields(node.id, status="archived", properties=properties)


def _archive_edge(store: GraphStore, edge: MemoryEdge) -> None:
    properties = dict(edge.properties)
    if properties.get("status") == "archived":
        return
    properties["status"] = "archived"
    store.update_edge_fields(edge.id, properties=properties)


def _mark_artifact_compiled(store: GraphStore, artifact: SourceArtifact, parse_result: DocumentParseResult) -> None:
    node = store.get_node(artifact.id)
    if node is None:
        return
    properties = dict(node.properties)
    now = utcnow_iso()
    status = "active"
    if parse_result.errors:
        status = "partially_readable"
    if parse_result.metadata.get("needs_ocr"):
        status = "needs_ocr"
    if parse_result.metadata.get("status") == "needs_parser":
        status = "needs_parser"
    properties.update(
        {
            "last_compiled_at": now,
            "status": status,
            "context_scope": artifact_context_scope(artifact),
            "parser_name": parse_result.parser_name,
            "parser_version": parse_result.parser_version,
            "parser_metadata": dict(parse_result.metadata),
            "parser_errors": list(parse_result.errors),
            "fragment_count": len(parse_result.fragments),
        }
    )
    store.update_node_fields(node.id, status=status if status in {"active", "archived"} else "active", properties=properties)


def _uri_node(uri: str, label: str) -> MemoryNode:
    return MemoryNode(
        id=stable_id("uri", uri),

        type="URI",
        label=label,
        text=uri,
        canonical_key=uri,
        properties={"uri": uri},
        salience=0.05,
        confidence=1.0,
        status="active",
    )


def _concept_node(artifact: SourceArtifact, fragment: DocumentFragment, parse_result: DocumentParseResult) -> MemoryNode | None:
    label = str(fragment.text or "").strip()
    if fragment.fragment_type != "heading" or len(label) < 2:
        return None
    key = f"{artifact.project_id}:concept:{artifact.relative_path}:{fragment.section_path or label}"
    return MemoryNode(
        id=stable_id("concept", artifact.project_id, artifact.relative_path, fragment.section_path or label),

        type="Concept",
        label=label,
        text=label,
        canonical_key=key,
        properties={
            "project_id": artifact.project_id,
            "artifact_id": artifact.id,
            "relative_path": artifact.relative_path,
            "context_scope": artifact_context_scope(artifact),
            "name": label,
            "section_path": fragment.section_path,
            "fragment_id": fragment.id,
            "fragment_type": fragment.fragment_type,
            "document_title": parse_result.title,
            "line_start": fragment.start_line,
            "line_end": fragment.end_line,
            "mode": "compile",
            "is_technical": True,
            "is_semantic": False,
        },
        salience=0.13,
        confidence=fragment.confidence,
        status="active",
    )


def _document_term_node(artifact: SourceArtifact, term: DocumentTerm, output: DocumentProcessingResult) -> MemoryNode:
    semantic_key = _document_semantic_key(term.key) or _document_semantic_key(term.label)
    node_id = stable_id("document_term", artifact.project_id, artifact.id, semantic_key)
    canonical_key = f"{artifact.project_id}:document_concept:{artifact.relative_path}:{semantic_key}"
    return MemoryNode(
        id=node_id,

        type="Concept",
        label=term.label,
        text=term.evidence or term.label,
        canonical_key=canonical_key,
        properties={
            "project_id": artifact.project_id,
            "artifact_id": artifact.id,
            "relative_path": artifact.relative_path,
            "context_scope": artifact_context_scope(artifact),
            "name": term.label,
            "semantic_key": semantic_key,
            "entity_type": term.term_type,
            "extractor": "document_processor",
            "source_layer": "project_document",
            "mode": "compile",
            "is_technical": False,
            "is_semantic": True,
            "evidence": term.evidence,
            "rank": term.rank,
            "term_frequency": term.term_frequency,
            "fragment_count": term.fragment_count,
            "raw_event_count": len(term.raw_events),
            "processor_signature": dict(output.signature),
        },
        salience=0.12 + term.rank * 0.24,
        confidence=0.55 + term.rank * 0.4,
        status="active",
    )


def _document_semantic_key(value: str) -> str:
    normalized = str(value or "").casefold()
    semantic_key = re.sub(r"\W+", "_", normalized, flags=re.UNICODE).strip("_")
    return semantic_key or canonicalize(value).replace(" ", "_")


def _document_raw_event_node(artifact: SourceArtifact, event: DocumentRawEvent) -> MemoryNode:
    node_id = stable_id("document_raw_event", artifact.project_id, artifact.id, event.id_key)
    canonical_key = f"{artifact.project_id}:document_raw_event:{artifact.relative_path}:{event.id_key}"
    return MemoryNode(
        id=node_id,
        type="RawEvent",
        label=f"{event.event_type}:{event.term_label}",
        text=event.evidence,
        canonical_key=canonical_key,
        properties={
            "project_id": artifact.project_id,
            "artifact_id": artifact.id,
            "relative_path": artifact.relative_path,
            "context_scope": artifact_context_scope(artifact),
            "name": event.term_label,
            "event_type": event.event_type,
            "term_key": event.term_key,
            "term_label": event.term_label,
            "fragment_id": event.fragment_id,
            "occurrence_count": event.occurrence_count,
            "rank": event.rank,
            "line_start": event.start_line,
            "line_end": event.end_line,
            "extractor": "document_processor",
            "source_layer": "project_document",
            "mode": "compile",
            "is_technical": False,
            "is_semantic": True,
            "evidence": event.evidence,
        },
        salience=0.04 + event.rank * 0.12,
        confidence=0.75,
        status="active",
    )


def _document_processing_edge_properties(artifact: SourceArtifact, *, evidence: str, rank: float) -> dict[str, Any]:
    return {
        "project_id": artifact.project_id,
        "artifact_id": artifact.id,
        "relative_path": artifact.relative_path,
        "context_scope": artifact_context_scope(artifact),
        "extractor": "document_processor",
        "source_layer": "project_document",
        "mode": "compile",
        "is_semantic": True,
        "is_technical": False,
        "evidence": evidence,
        "rank": rank,
    }


def _typed_edge(

    from_id: str,
    to_id: str,
    type_: str,
    properties: dict[str, Any],
    *,
    source_file: str | None = None,
    line_start: int | None = None,
    line_end: int | None = None,
    extractor: str | None = None,
    evidence: str | None = None,
    is_semantic: bool = False,
    is_technical: bool = True,
) -> MemoryEdge:
    now = utcnow_iso()
    props = dict(properties)
    props.update(
        {
            "source_id": from_id,
            "target_id": to_id,
            "type": type_,
            "confidence": 1.0,
            "source_file": source_file or str(properties.get("relative_path") or properties.get("source_file") or ""),
            "line_start": line_start,
            "line_end": line_end if line_end is not None else line_start,
            "extractor": extractor or str(properties.get("parser_name") or "artifact_compiler"),
            "evidence": evidence or str(properties.get("evidence") or type_),
            "created_at": now,
            "updated_at": now,
            "mode": "compile",
            "is_semantic": is_semantic,
            "is_technical": is_technical,
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


def _track_node(result: ArtifactCompilationResult, node_id: str, created: bool) -> None:
    result.affected_node_ids.add(node_id)
    if created:
        result.added_nodes.append(node_id)
    else:
        result.updated_nodes.append(node_id)


def _track_edge(result: ArtifactCompilationResult, edge_id: str, created: bool) -> None:
    result.affected_edge_ids.add(edge_id)
    if created:
        result.added_edges.append(edge_id)
    else:
        result.updated_edges.append(edge_id)


def _upsert_edges_deduped(store: GraphStore, result: ArtifactCompilationResult, edges: list[MemoryEdge]) -> None:
    if not edges:
        return
    by_pattern: dict[tuple[str, str, str, str], MemoryEdge] = {}
    for edge in edges:
        by_pattern[(edge.from_id, edge.type, edge.to_id)] = edge
    for stored, created in store.batch_upsert_edges(list(by_pattern.values())):
        _track_edge(result, stored.id, created)


class _SymbolNodeResolver:
    def __init__(self, symbol_nodes: dict[str, MemoryNode]) -> None:
        self.by_qualified_name = dict(symbol_nodes)
        self.by_tail: dict[str, MemoryNode] = {}
        for qualified_name, node in symbol_nodes.items():
            tail = qualified_name.split(".")[-1]
            if tail not in self.by_tail:
                self.by_tail[tail] = node

    def resolve(self, name: str | None) -> MemoryNode | None:
        if not name:
            return None
        if name in self.by_qualified_name:
            return self.by_qualified_name[name]
        return self.by_tail.get(name.split(".")[-1])

    def is_interface(self, name: str | None) -> bool:
        target = self.resolve(name)
        if target is not None:
            return target.type == "Interface" or bool(target.properties.get("is_interface"))
        return str(name or "").split(".")[-1] == "Protocol"


def _is_code_artifact(artifact: SourceArtifact) -> bool:
    return artifact.artifact_type == "code"


def _is_document_artifact(artifact: SourceArtifact) -> bool:
    return artifact.artifact_type in {"markdown", "text", "pdf", "config", "data", "unknown"}


def _normalize_document_policies(policies: list[dict[str, object]] | None) -> dict[str, dict[str, object]]:
    normalized: dict[str, dict[str, object]] = {}
    for item in policies or []:
        format_name = str(item.get("format") or "").strip().casefold()
        if not format_name:
            continue
        extensions = item.get("extensions", [])
        filenames = item.get("filenames", [])
        normalized[format_name] = {
            "extensions": [str(extension).strip().casefold() for extension in extensions if str(extension).strip()],
            "filenames": [str(filename).strip().casefold() for filename in filenames if str(filename).strip()],
            "ingest": bool(item.get("ingest", True)),
        }
    return normalized


def _document_policy_for_artifact(policies: dict[str, dict[str, object]], artifact: SourceArtifact) -> dict[str, object] | None:
    if not policies:
        return None
    path = Path(artifact.relative_path or artifact.path)
    suffix = path.suffix.casefold()
    filename = path.name.casefold()
    for policy in policies.values():
        extensions = policy.get("extensions", [])
        filenames = policy.get("filenames", [])
        if isinstance(extensions, list) and suffix and suffix in extensions:
            return policy
        if isinstance(filenames, list) and filename in filenames:
            return policy
    return {"ingest": False}


def _is_test_artifact(artifact: SourceArtifact) -> bool:
    rel = artifact.relative_path.replace("\\", "/")
    name = Path(rel).name
    return artifact.artifact_type == "code" and (name.startswith("test_") or name.endswith("_test.py") or rel.startswith("tests/"))


def _is_package_init_artifact(artifact: SourceArtifact) -> bool:
    rel = artifact.relative_path.replace("\\", "/")
    return artifact.artifact_type == "code" and (rel == "__init__.py" or rel.endswith("/__init__.py"))


def _document_result_from_code(artifact: SourceArtifact, code_result: CodeParseResult, source_text: str | None = None) -> DocumentParseResult:
    lines = (source_text if source_text is not None else Path(artifact.path).read_text(encoding="utf-8", errors="replace")).splitlines()
    fragments: list[DocumentFragment] = []
    if code_result.parser_name == "none":
        return DocumentParseResult(
            title=code_result.module.name,
            metadata={"language": code_result.module.language, "code_parser": code_result.parser_name, "module": code_result.module.to_dict(), "status": "unsupported_language"},
            fragments=[],
            links=[],
            tables=[],
            errors=list(code_result.errors),
            parser_name=code_result.parser_name,
            parser_version=code_result.parser_version,
        )
    index = 0
    for symbol in code_result.symbols:
        if symbol.kind not in {"class", "function", "method", "async_function", "async_method"}:
            continue
        start = symbol.start_line or 1
        end = symbol.end_line or start
        text = "\n".join(lines[start - 1 : end]).strip() if lines else symbol.name
        fragments.append(
            make_fragment(
                artifact_id=artifact.id,
                fragment_type="code_block",
                text=text,
                index=index,
                start_line=start,
                end_line=end,
                section_path=symbol.qualified_name,
                metadata={
                    "symbol_id": symbol.id,
                    "symbol_kind": symbol.kind,
                    "symbol_name": symbol.name,
                    "symbol_qualified_name": symbol.qualified_name,
                    "parser": code_result.parser_name,
                },
            )
        )
        index += 1
    if not fragments:
        fragments.append(
            make_fragment(
                artifact_id=artifact.id,
                fragment_type="code_block",
                text="\n".join(lines).strip() if lines else f"Code artifact: {artifact.relative_path}",
                index=0,
                start_line=1 if lines else None,
                end_line=len(lines) if lines else None,
                section_path=code_result.module.name,
                metadata={"symbol_id": code_result.module.id, "symbol_kind": "module", "symbol_name": code_result.module.name, "symbol_qualified_name": code_result.module.name, "parser": code_result.parser_name},
            )
        )
    return DocumentParseResult(
        title=code_result.module.name,
        metadata={"language": code_result.module.language, "code_parser": code_result.parser_name, "module": code_result.module.to_dict()},
        fragments=fragments,
        links=[],
        tables=[],
        errors=list(code_result.errors),
        parser_name=code_result.parser_name,
        parser_version=code_result.parser_version,
    )


def _empty_code_result(artifact: SourceArtifact, error: str | None) -> CodeParseResult:
    module = CodeModule(
        id=stable_id("module", artifact.id),
        artifact_id=artifact.id,
        name=Path(artifact.relative_path).stem,
        path=artifact.relative_path,
        language=artifact.language or "unknown",
    )
    return CodeParseResult(
        module=module,
        symbols=[],
        imports=[],
        calls=[],
        references=[],
        classes=[],
        functions=[],
        methods=[],
        comments=[],
        docstrings=[],
        errors=[error] if error else [],
        parser_name="none",
        parser_version="none",
    )


def _skipped_document_parse_result(artifact: SourceArtifact) -> DocumentParseResult:
    return DocumentParseResult(
        title=Path(artifact.path).stem,
        metadata={"status": "document_ingest_disabled", "artifact_type": artifact.artifact_type},
        fragments=[],
        links=[],
        tables=[],
        errors=[],
        parser_name="document_ingest_disabled",
        parser_version="disabled",
    )


def _disabled_parse_result(artifact: SourceArtifact, parser_name: str, error: str) -> DocumentParseResult:
    return DocumentParseResult(
        title=Path(artifact.path).stem,
        metadata={"status": "needs_parser", "parser_disabled": True, "artifact_type": artifact.artifact_type},
        fragments=[
            make_fragment(
                artifact_id=artifact.id,
                fragment_type="metadata",
                text=f"{artifact.artifact_type} artifact not parsed: {Path(artifact.path).name}",
                index=0,
                metadata={"parser": parser_name, "status": "needs_parser", "parser_disabled": True},
                confidence=0.5,
            )
        ],
        links=[],
        tables=[],
        errors=[error],
        parser_name=parser_name,
        parser_version="disabled",
    )


def _module_node(artifact: SourceArtifact, module: CodeModule, code_result: CodeParseResult) -> MemoryNode:
    properties = module.to_dict()
    properties.update(
        {
            "project_id": artifact.project_id,
            "relative_path": artifact.relative_path,
            "context_scope": artifact_context_scope(artifact),
            "parser_name": code_result.parser_name,
            "parser_version": code_result.parser_version,
            "name": module.name,
            "mode": "compile",
            "is_technical": True,
            "is_semantic": False,
        }
    )
    return MemoryNode(
        id=module.id,

        type="Module",
        label=module.name,
        text=module.path,
        canonical_key=f"{artifact.id}:module",
        properties=properties,
        salience=0.15,
        confidence=1.0,
        status="active",
    )


def _symbol_node(artifact: SourceArtifact, symbol: CodeSymbol, code_result: CodeParseResult) -> MemoryNode:
    node_type = _symbol_node_type(symbol)
    properties = symbol.to_dict()
    properties.update(
        {
            "project_id": artifact.project_id,
            "relative_path": artifact.relative_path,
            "context_scope": artifact_context_scope(artifact),
            "parser_name": code_result.parser_name,
            "parser_version": code_result.parser_version,
            "mode": "compile",
            "is_technical": True,
            "is_semantic": False,
        }
    )
    return MemoryNode(
        id=symbol.id,

        type=node_type,
        label=symbol.qualified_name,
        text=symbol.docstring,
        canonical_key=f"{artifact.id}:{symbol.kind}:{symbol.qualified_name}",
        properties=properties,
        salience=0.18,
        confidence=1.0,
        status="active",
    )


def _symbol_node_type(symbol: CodeSymbol) -> str:
    if symbol.metadata.get("is_interface"):
        return "Interface"
    if symbol.kind == "class":
        return "Class"
    if symbol.kind in {"function", "async_function"}:
        return "Function"
    if symbol.kind in {"method", "async_method"}:
        return "Method"
    if symbol.kind == "variable":
        return "Variable"
    return "CodeSymbol"


def _symbol_edges(
    artifact: SourceArtifact,
    file_id: str,
    module_id: str,
    symbol: CodeSymbol,
    node_id: str,
    symbol_nodes: dict[str, MemoryNode],
    extractor: str,
) -> list[MemoryEdge]:
    edges = [
        _typed_edge(artifact.id, node_id, "DEFINES", {"project_id": artifact.project_id, "artifact_id": artifact.id, "name": symbol.name, "kind": symbol.kind}, source_file=artifact.relative_path, line_start=symbol.start_line, line_end=symbol.end_line, extractor=extractor, evidence=symbol.qualified_name),
        _typed_edge(file_id, node_id, "DEFINES", {"project_id": artifact.project_id, "artifact_id": artifact.id, "name": symbol.name, "kind": symbol.kind}, source_file=artifact.relative_path, line_start=symbol.start_line, line_end=symbol.end_line, extractor=extractor, evidence=symbol.qualified_name),
        _typed_edge(module_id, node_id, "CONTAINS", {"project_id": artifact.project_id, "artifact_id": artifact.id, "name": symbol.name, "kind": symbol.kind}, source_file=artifact.relative_path, line_start=symbol.start_line, line_end=symbol.end_line, extractor=extractor, evidence=symbol.qualified_name),
    ]
    parent = symbol_nodes.get(symbol.parent_qualified_name or "")
    if parent:
        edges.append(_typed_edge(parent.id, node_id, "DEFINES", {"project_id": artifact.project_id, "artifact_id": artifact.id, "name": symbol.name, "kind": symbol.kind}, source_file=artifact.relative_path, line_start=symbol.start_line, line_end=symbol.end_line, extractor=extractor, evidence=symbol.qualified_name))
        edges.append(_typed_edge(parent.id, node_id, "CONTAINS", {"project_id": artifact.project_id, "artifact_id": artifact.id, "name": symbol.name, "kind": symbol.kind}, source_file=artifact.relative_path, line_start=symbol.start_line, line_end=symbol.end_line, extractor=extractor, evidence=symbol.qualified_name))
        if symbol.kind in {"method", "async_method"} and parent.type in {"Class", "Interface"}:
            edges.append(_typed_edge(parent.id, node_id, "METHOD", {"project_id": artifact.project_id, "artifact_id": artifact.id, "name": symbol.name, "kind": symbol.kind}, source_file=artifact.relative_path, line_start=symbol.start_line, line_end=symbol.end_line, extractor=extractor, evidence=symbol.qualified_name))
    return edges


def _import_node(artifact: SourceArtifact, item: CodeImport) -> MemoryNode:
    properties = item.to_dict()
    properties.update(
        {
            "project_id": artifact.project_id,
            "relative_path": artifact.relative_path,
            "context_scope": artifact_context_scope(artifact),
            "name": item.name or item.module,
            "is_re_export": _is_package_init_artifact(artifact),
            "mode": "compile",
            "is_technical": True,
            "is_semantic": False,
        }
    )
    return MemoryNode(
        id=item.id,

        type="Import",
        label=item.raw or item.module or item.name,
        canonical_key=f"{artifact.id}:import:{item.id}",
        properties=properties,
        salience=0.08,
        confidence=1.0,
        status="active",
    )


def _code_text_node(artifact: SourceArtifact, item: CodeText) -> MemoryNode:
    node_type = "Docstring" if item.kind == "docstring" else "Comment"
    properties = item.to_dict()
    properties.update(
        {
            "project_id": artifact.project_id,
            "relative_path": artifact.relative_path,
            "context_scope": artifact_context_scope(artifact),
            "name": item.kind,
            "mode": "compile",
            "is_technical": True,
            "is_semantic": False,
        }
    )
    return MemoryNode(
        id=item.id,

        type=node_type,
        label=f"{item.kind}:{item.start_line}",
        text=item.text,
        canonical_key=f"{artifact.id}:{item.kind}:{item.id}",
        properties=properties,
        salience=0.05,
        confidence=1.0,
        status="active",
    )


def _file_id(artifact: SourceArtifact) -> str:
    return stable_id("file", artifact.project_id, artifact.relative_path)


def _config_node(artifact: SourceArtifact) -> MemoryNode:
    return MemoryNode(
        id=stable_id("config", artifact.project_id, artifact.relative_path),

        type="Config",
        label=artifact.relative_path,
        canonical_key=f"{artifact.project_id}:config:{artifact.relative_path}",
        properties={
            "project_id": artifact.project_id,
            "artifact_id": artifact.id,
            "relative_path": artifact.relative_path,
            "context_scope": artifact_context_scope(artifact),
            "language": artifact.language,
            "mode": "compile",
            "is_technical": True,
            "is_semantic": False,
        },
        salience=0.12,
        confidence=1.0,
        status="active",
    )


def _test_node(artifact: SourceArtifact) -> MemoryNode:
    return MemoryNode(
        id=stable_id("test", artifact.project_id, artifact.relative_path),

        type="Test",
        label=artifact.relative_path,
        canonical_key=f"{artifact.project_id}:test:{artifact.relative_path}",
        properties={
            "project_id": artifact.project_id,
            "artifact_id": artifact.id,
            "relative_path": artifact.relative_path,
            "context_scope": artifact_context_scope(artifact),
            "language": artifact.language,
            "mode": "compile",
            "is_technical": True,
            "is_semantic": False,
        },
        salience=0.16,
        confidence=1.0,
        status="active",
    )


def _schema_node(artifact: SourceArtifact, symbol: CodeSymbol) -> MemoryNode:
    return MemoryNode(
        id=stable_id("schema", artifact.project_id, symbol.qualified_name),

        type="Schema",
        label=symbol.qualified_name,
        canonical_key=f"{artifact.project_id}:schema:{symbol.qualified_name}",
        properties={
            "project_id": artifact.project_id,
            "artifact_id": artifact.id,
            "relative_path": artifact.relative_path,
            "context_scope": artifact_context_scope(artifact),
            "name": symbol.name,
            "qualified_name": symbol.qualified_name,
            "line_start": symbol.start_line,
            "line_end": symbol.end_line,
            "mode": "compile",
            "is_technical": True,
            "is_semantic": False,
        },
        salience=0.18,
        confidence=1.0,
        status="active",
    )


def _dependency_node(artifact: SourceArtifact, item: CodeImport) -> MemoryNode:
    name = item.module or item.name or "unknown"
    return MemoryNode(
        id=stable_id("dependency", artifact.project_id, name),

        type="Dependency",
        label=name,
        canonical_key=f"{artifact.project_id}:dependency:{name}",
        properties={
            "project_id": artifact.project_id,
            "artifact_id": artifact.id,
            "context_scope": artifact_context_scope(artifact),
            "name": name,
            "module": item.module,
            "import_name": item.name,
            "external": True,
            "mode": "compile",
            "is_technical": True,
            "is_semantic": False,
        },
        salience=0.08,
        confidence=1.0,
        status="active",
    )


def _endpoint_node_for_decorator(artifact: SourceArtifact, symbol: CodeSymbol, decorator: str) -> MemoryNode | None:
    parsed = _parse_route_decorator(decorator)
    if parsed is None:
        return None
    method, path = parsed
    endpoint_id = stable_id("endpoint", artifact.project_id, method, path)
    return MemoryNode(
        id=endpoint_id,

        type="Endpoint",
        label=f"{method} {path}".strip(),
        canonical_key=f"{artifact.project_id}:endpoint:{method}:{path}",
        properties={
            "project_id": artifact.project_id,
            "artifact_id": artifact.id,
            "relative_path": artifact.relative_path,
            "context_scope": artifact_context_scope(artifact),
            "handler": symbol.qualified_name,
            "method": method,
            "path": path,
            "line_start": symbol.start_line,
            "line_end": symbol.end_line,
            "mode": "compile",
            "is_technical": True,
            "is_semantic": False,
        },
        salience=0.16,
        confidence=1.0,
        status="active",
    )


def _parse_route_decorator(decorator: str) -> tuple[str, str] | None:
    value = decorator.strip()
    if "(" not in value or ")" not in value:
        return None
    head = value.split("(", 1)[0]
    tail = value.split("(", 1)[1]
    method = head.split(".")[-1].upper()
    if method == "ROUTE":
        method = "ANY"
    if method not in {"ANY", "GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD", "WEBSOCKET"}:
        return None
    quote_positions = [index for index, char in enumerate(tail) if char in {"'", '"'}]
    if len(quote_positions) < 2:
        return None
    start = quote_positions[0]
    quote = tail[start]
    end = tail.find(quote, start + 1)
    if end <= start:
        return None
    path = tail[start + 1 : end]
    if not path.startswith("/"):
        return None
    return method, path


def _external_code_symbol_node(artifact: SourceArtifact, name: str, kind: str) -> MemoryNode:
    clean_name = _clean_external_symbol_name(name)
    if clean_name is None:
        raise ValueError("external code symbol name must be meaningful")
    return MemoryNode(
        id=stable_id("external-code-symbol", artifact.project_id, clean_name, kind),

        type="CodeSymbol",
        label=clean_name,
        canonical_key=f"external:{artifact.project_id}:{clean_name}:{kind}",
        properties={
            "project_id": artifact.project_id,
            "artifact_id": artifact.id,
            "context_scope": artifact_context_scope(artifact),
            "name": clean_name,
            "qualified_name": clean_name,
            "kind": kind,
            "external": True,
            "synthetic": True,
            "mode": "compile",
            "is_technical": True,
            "is_semantic": False,
        },
        salience=0.01,
        confidence=0.5,
        status="active",
    )


def _should_persist_variable(symbol: CodeSymbol, read_references: list[Any]) -> bool:
    if symbol.kind != "variable":
        return True
    if "." in symbol.name:
        return True
    parent = symbol.parent_qualified_name or ""
    if not parent:
        return True
    if parent.count(".") <= 0:
        return True
    if symbol.name.isupper():
        return True
    return not _variable_has_read_reference(symbol, read_references)


def _should_record_unresolved_call(target: str | None, *, imported_names: set[str] | None = None) -> bool:
    clean = _clean_call_target(target)
    if clean is None:
        return False
    if "." in clean:
        head = clean.split(".", 1)[0]
        return head not in (imported_names or set()) and head not in LANGUAGE_BUILTIN_GLOBALS
    return clean not in (imported_names or set()) and clean not in LANGUAGE_BUILTIN_GLOBALS


def _store_unresolved_call_summaries(store: GraphStore, calls_by_owner: dict[str, list[dict[str, Any]]]) -> None:
    for owner_id, calls in calls_by_owner.items():
        if not calls:
            continue
        owner = store.get_node(owner_id)
        if owner is None or owner.status == "archived":
            continue
        deduped: dict[tuple[str, object], dict[str, Any]] = {}
        for call in calls:
            target = str(call.get("target") or "").strip()
            if not target:
                continue
            deduped.setdefault((target, call.get("line")), call)
        properties = dict(owner.properties)
        properties["unresolved_calls"] = sorted(
            deduped.values(),
            key=lambda item: (str(item.get("target") or ""), int(item.get("line") or 0)),
        )
        properties["unresolved_call_count"] = len(deduped)
        properties["updated_at"] = utcnow_iso()
        store.update_node_fields(owner.id, properties=properties)


def _static_analysis_finding_nodes(
    artifact: SourceArtifact,
    code_result: CodeParseResult,
    symbol_nodes: dict[str, MemoryNode],
    import_node_ids: dict[str, str],
    table: SymbolTable,
) -> list[tuple[MemoryNode, str]]:
    findings: list[tuple[MemoryNode, str]] = []
    used_symbol_names = _used_symbol_qualified_names(code_result, table)
    read_references = [reference for reference in code_result.references if reference.access in {"read", "return", "raise"}]
    parent_symbols = {symbol.qualified_name: symbol for symbol in code_result.symbols}

    for symbol in code_result.symbols:
        node = symbol_nodes.get(symbol.qualified_name)
        if node is None or _should_skip_unused_symbol(symbol):
            continue
        if symbol.kind == "variable":
            if not _is_local_variable(symbol, parent_symbols):
                continue
            if not _variable_has_read_reference(symbol, read_references):
                confidence = 0.45 if _is_test_artifact(artifact) else 0.8
                evidence_scope = "test_local_artifact" if _is_test_artifact(artifact) else "local_artifact"
                findings.append(
                    (
                        _static_analysis_finding_node(
                            artifact,
                            finding_type="unused_variable",
                            symbol=symbol,
                            symbol_node=node,
                            reason=f"Variable {symbol.qualified_name} is written but has no detected read in this artifact.",
                            confidence=confidence,
                            evidence_scope=evidence_scope,
                        ),
                        node.id,
                    )
                )
            continue
        if symbol.kind in {"function", "async_function", "method", "async_method", "class"} and symbol.qualified_name not in used_symbol_names:
            if _is_test_artifact(artifact):
                continue
            confidence = 0.55
            evidence_scope = "local_artifact"
            reason = f"{_finding_symbol_kind(symbol).capitalize()} {symbol.qualified_name} has no detected local call, reference, inheritance, or instantiation in this artifact."
            if _is_public_api_risk_symbol(symbol):
                confidence = 0.35
                evidence_scope = "public_api_local_artifact"
                reason = (
                    f"{_finding_symbol_kind(symbol).capitalize()} {symbol.qualified_name} has no detected local call, reference, "
                    "inheritance, or instantiation in this artifact, but it is public enough to require API/callback validation before removal."
                )
            findings.append(
                (
                    _static_analysis_finding_node(
                        artifact,
                        finding_type=f"possibly_unused_{_finding_symbol_kind(symbol)}",
                        symbol=symbol,
                        symbol_node=node,
                        reason=reason,
                        confidence=confidence,
                        evidence_scope=evidence_scope,
                    ),
                    node.id,
                )
            )

    usage_names = _import_usage_names(code_result)
    for item in code_result.imports:
        node_id = import_node_ids.get(item.id)
        if node_id is None or _should_skip_unused_import(artifact, item):
            continue
        exposed_name = _import_exposed_name(item)
        if exposed_name is None:
            continue
        if not _name_is_used(exposed_name, usage_names):
            confidence = 0.45 if _is_test_artifact(artifact) else 0.8
            evidence_scope = "test_local_artifact" if _is_test_artifact(artifact) else "local_artifact"
            findings.append(
                (
                    _static_analysis_import_finding_node(
                        artifact,
                        item,
                        node_id,
                        reason=f"Import {exposed_name} has no detected reference in this artifact.",
                        confidence=confidence,
                        evidence_scope=evidence_scope,
                    ),
                    node_id,
                )
            )
    return findings


def _static_analysis_finding_node(
    artifact: SourceArtifact,
    *,
    finding_type: str,
    symbol: CodeSymbol,
    symbol_node: MemoryNode,
    reason: str,
    confidence: float,
    evidence_scope: str,
) -> MemoryNode:
    label = f"{finding_type}: {symbol.qualified_name}"
    cleanup_priority = _cleanup_priority(finding_type, confidence, evidence_scope)
    rationale = _cleanup_rationale(
        finding_type=finding_type,
        evidence_scope=evidence_scope,
        cleanup_priority=cleanup_priority,
        symbol_kind=symbol.kind,
        symbol_name=symbol.name,
    )
    properties = {
        "project_id": artifact.project_id,
        "artifact_id": artifact.id,
        "relative_path": artifact.relative_path,
        "context_scope": artifact_context_scope(artifact),
        "finding_type": finding_type,
        "category": "dead_code",
        "severity": "info",
        "reason": reason,
        "evidence_scope": evidence_scope,
        "confidence": confidence,
        "cleanup_priority": cleanup_priority,
        "cleanup_rank": _cleanup_rank(cleanup_priority),
        **rationale,
        "symbol_id": symbol_node.id,
        "symbol_type": symbol_node.type,
        "symbol_kind": symbol.kind,
        "symbol_name": symbol.name,
        "qualified_name": symbol.qualified_name,
        "name": symbol.name,
        "line_start": symbol.start_line,
        "line_end": symbol.end_line,
        "mode": "compile",
        "is_technical": True,
        "is_semantic": False,
    }
    return MemoryNode(
        id=stable_id("static-analysis-finding", artifact.id, finding_type, symbol.qualified_name),

        type="StaticAnalysisFinding",
        label=label,
        text=reason,
        canonical_key=f"{artifact.id}:finding:{finding_type}:{symbol.qualified_name}",
        properties=properties,
        salience=0.14,
        confidence=confidence,
        status="active",
    )


def _static_analysis_import_finding_node(
    artifact: SourceArtifact,
    item: CodeImport,
    import_node_id: str,
    *,
    reason: str,
    confidence: float,
    evidence_scope: str,
) -> MemoryNode:
    exposed_name = _import_exposed_name(item) or item.raw or "import"
    label = f"unused_import: {exposed_name}"
    cleanup_priority = _cleanup_priority("unused_import", confidence, evidence_scope)
    rationale = _cleanup_rationale(
        finding_type="unused_import",
        evidence_scope=evidence_scope,
        cleanup_priority=cleanup_priority,
        symbol_kind="import",
        symbol_name=exposed_name,
    )
    properties = {
        "project_id": artifact.project_id,
        "artifact_id": artifact.id,
        "relative_path": artifact.relative_path,
        "context_scope": artifact_context_scope(artifact),
        "finding_type": "unused_import",
        "category": "dead_code",
        "severity": "info",
        "reason": reason,
        "evidence_scope": evidence_scope,
        "confidence": confidence,
        "cleanup_priority": cleanup_priority,
        "cleanup_rank": _cleanup_rank(cleanup_priority),
        **rationale,
        "symbol_id": import_node_id,
        "symbol_type": "Import",
        "symbol_kind": "import",
        "symbol_name": exposed_name,
        "qualified_name": exposed_name,
        "name": exposed_name,
        "module": item.module,
        "import_name": item.name,
        "alias": item.alias,
        "line_start": item.line,
        "line_end": item.line,
        "mode": "compile",
        "is_technical": True,
        "is_semantic": False,
    }
    return MemoryNode(
        id=stable_id("static-analysis-finding", artifact.id, "unused_import", item.id),

        type="StaticAnalysisFinding",
        label=label,
        text=reason,
        canonical_key=f"{artifact.id}:finding:unused_import:{item.id}",
        properties=properties,
        salience=0.13,
        confidence=confidence,
        status="active",
    )


def _cleanup_priority(finding_type: str, confidence: float, evidence_scope: str) -> str:
    if finding_type in {"unused_import", "unused_variable"} and confidence >= 0.75 and evidence_scope == "local_artifact":
        return "high"
    if evidence_scope == "local_artifact":
        return "medium"
    return "low"


def _cleanup_rank(cleanup_priority: str) -> int:
    return {"high": 3, "medium": 2, "low": 1}.get(cleanup_priority, 0)


def _cleanup_rationale(
    *,
    finding_type: str,
    evidence_scope: str,
    cleanup_priority: str,
    symbol_kind: str,
    symbol_name: str,
) -> dict[str, object]:
    blocking_signals: list[str] = []
    validation_reason = ""
    if evidence_scope == "public_api_local_artifact":
        blocking_signals.extend(["public_api", "dynamic_reference_unknown"])
        validation_reason = "Validate public API, callbacks, configuration, and documentation before removing this symbol."
    elif evidence_scope == "test_local_artifact":
        blocking_signals.append("test_artifact")
        validation_reason = "Validate the test fixture intent before removing this local test-only finding."
    elif finding_type.startswith("possibly_unused_"):
        blocking_signals.append("dynamic_reference_unknown")
        validation_reason = "Validate project-wide dynamic references before removing this symbol."

    if symbol_kind in {"method", "async_method"} and symbol_name in {"setUp", "tearDown", "setUpClass", "tearDownClass", "asyncSetUp", "asyncTearDown"}:
        blocking_signals.append("framework_lifecycle")
        validation_reason = "Validate framework lifecycle usage before removing this method."
    if symbol_name in {"main", "cli_main"}:
        blocking_signals.append("entrypoint")
        validation_reason = "Validate CLI/script entrypoint usage before removing this symbol."

    if cleanup_priority == "high" and not blocking_signals:
        return {
            "removal_safety": "safe",
            "removal_reason": f"{finding_type} is local to this artifact with high confidence and no public-surface signal.",
            "validation_reason": "",
            "blocking_signals": [],
        }
    if "public_api" in blocking_signals or "entrypoint" in blocking_signals or "framework_lifecycle" in blocking_signals:
        safety = "risky"
    else:
        safety = "validate"
    return {
        "removal_safety": safety,
        "removal_reason": f"{finding_type} has no detected local usage, but removal needs validation before editing.",
        "validation_reason": validation_reason or "Validate references outside the local artifact before removing this candidate.",
        "blocking_signals": sorted(set(blocking_signals)),
    }


def _used_symbol_qualified_names(code_result: CodeParseResult, table: SymbolTable) -> set[str]:
    used: set[str] = set()
    for call in code_result.calls:
        target = table.resolve_call_target(call.target, caller=call.caller)
        if target is not None:
            used.add(target.qualified_name)
    for reference in code_result.references:
        if reference.access == "write":
            continue
        target = table.resolve_call_target(reference.name, caller=reference.owner)
        if target is not None:
            used.add(target.qualified_name)
    for symbol in code_result.symbols:
        for base in symbol.bases:
            target = table.resolve_call_target(base, caller=symbol.parent_qualified_name)
            if target is not None:
                used.add(target.qualified_name)
        if symbol.returns:
            target = table.resolve_call_target(symbol.returns, caller=symbol.parent_qualified_name)
            if target is not None:
                used.add(target.qualified_name)
    return used


def _variable_has_read_reference(symbol: CodeSymbol, references: list[Any]) -> bool:
    if "." in symbol.name:
        return True
    for reference in references:
        if not _reference_owner_can_see_symbol(reference.owner, symbol):
            continue
        if reference.name == symbol.name or str(reference.name).startswith(f"{symbol.name}."):
            return True
    return False


def _is_local_variable(symbol: CodeSymbol, parent_symbols: dict[str, CodeSymbol]) -> bool:
    parent = parent_symbols.get(symbol.parent_qualified_name or "")
    return parent is not None and parent.kind in {"function", "async_function", "method", "async_method"}


def _reference_owner_can_see_symbol(owner: str | None, symbol: CodeSymbol) -> bool:
    parent = symbol.parent_qualified_name or ""
    owner_value = owner or ""
    if not parent:
        return True
    if parent == owner_value:
        return True
    if symbol.kind == "variable" and parent.count(".") == 0:
        return True
    return owner_value.startswith(f"{parent}.")


def _should_skip_unused_symbol(symbol: CodeSymbol) -> bool:
    if symbol.name.startswith("_"):
        return True
    if symbol.name.startswith("__") and symbol.name.endswith("__"):
        return True
    if symbol.decorators:
        return True
    return False


def _is_public_api_risk_symbol(symbol: CodeSymbol) -> bool:
    if symbol.kind in {"method", "async_method"}:
        return True
    if symbol.kind == "class":
        return True
    return bool(symbol.name and not symbol.name.startswith("_"))


def _finding_symbol_kind(symbol: CodeSymbol) -> str:
    if symbol.kind in {"async_function", "function"}:
        return "function"
    if symbol.kind in {"async_method", "method"}:
        return "method"
    if symbol.kind == "class":
        return "class"
    return str(symbol.kind)


def _import_usage_names(code_result: CodeParseResult) -> set[str]:
    names: set[str] = set()
    for call in code_result.calls:
        _add_usage_name(names, call.target)
    for reference in code_result.references:
        if reference.access != "write":
            _add_usage_name(names, reference.name)
    for symbol in code_result.symbols:
        for decorator in symbol.decorators:
            _add_usage_name(names, decorator)
        for base in symbol.bases:
            _add_usage_name(names, base)
        if symbol.returns:
            _add_usage_name(names, symbol.returns)
            _add_annotation_usage_names(names, symbol.returns)
        for annotation in symbol.metadata.get("param_annotations") or []:
            _add_annotation_usage_names(names, str(annotation))
    return names


def _imported_binding_names(imports: list[CodeImport]) -> set[str]:
    names: set[str] = set()
    for item in imports:
        exposed = _import_exposed_name(item)
        if exposed:
            names.add(exposed)
        if item.module:
            names.add(str(item.module).split(".", 1)[0])
    return names


_ANNOTATION_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _add_annotation_usage_names(names: set[str], annotation: str | None) -> None:
    if not annotation:
        return
    for token in _ANNOTATION_NAME_RE.findall(annotation):
        _add_usage_name(names, token)


def _add_usage_name(names: set[str], value: str | None) -> None:
    if not value:
        return
    clean = str(value).strip().removeprefix("@")
    if not clean:
        return
    names.add(clean)
    names.add(clean.split(".", 1)[0])
    names.add(clean.split(".")[-1])
    callable_name = re.split(r"[\[(]", clean, maxsplit=1)[0].strip()
    if callable_name:
        names.add(callable_name)
        names.add(callable_name.split(".", 1)[0])
        names.add(callable_name.split(".")[-1])


def _import_exposed_name(item: CodeImport) -> str | None:
    if item.alias:
        return item.alias
    if item.name and item.name != "*":
        return item.name
    if item.module:
        return item.module.split(".")[0]
    return None


def _should_skip_unused_import(artifact: SourceArtifact, item: CodeImport) -> bool:
    if item.module == "__future__":
        return True
    if _is_package_init_artifact(artifact):
        return True
    return item.name == "*" or _import_exposed_name(item) is None


def _name_is_used(name: str, usage_names: set[str]) -> bool:
    return name in usage_names or any(candidate.startswith(f"{name}.") for candidate in usage_names)


def _valid_external_symbol_name(name: str | None) -> bool:
    return _clean_external_symbol_name(name) is not None


def _clean_external_symbol_name(name: str | None, *, imported_names: set[str] | None = None) -> str | None:
    value = _clean_call_target(name)
    if value is None:
        return None
    if not value or value.casefold() in {"none", "null", "undefined", "unknown", "nan"}:
        return None
    if _external_symbol_is_noise(value, imported_names=imported_names):
        return None
    return value


def _external_symbol_is_noise(value: str, *, imported_names: set[str] | None = None) -> bool:
    if value in LANGUAGE_BUILTIN_GLOBALS:
        return True
    head = re.split(r"[\[<({.]", value, maxsplit=1)[0].strip()
    if head and head in LANGUAGE_BUILTIN_GLOBALS:
        return True
    return bool(head and head in (imported_names or set()))


def _clean_call_target(name: str | None) -> str | None:
    if name is None:
        return None
    value = str(name).strip().removeprefix("@").strip()
    if not value:
        return None
    if "(" in value:
        value = value.split("(", 1)[0].strip()
    return value or None


def _reference_targets_local_argument(reference: Any, symbol_nodes: dict[str, MemoryNode]) -> bool:
    owner = symbol_nodes.get(getattr(reference, "owner", "") or "")
    if owner is None:
        return False
    args = {str(arg) for arg in owner.properties.get("args", [])}
    if not args:
        return False
    name = str(getattr(reference, "name", "") or "").split(".", 1)[0]
    return name in args


def _code_nodes_for_artifact(store: GraphStore, artifact: SourceArtifact | MemoryNode) -> list[MemoryNode]:
    return [
        node
        for node in store.find_nodes_by_property("artifact_id", artifact.id, limit=100000)
        if node.type in CODE_GRAPH_NODE_TYPES and node.properties.get("artifact_id") == artifact.id
    ]


def _code_related_edges(store: GraphStore, node: MemoryNode) -> list[MemoryEdge]:
    edges: dict[str, MemoryEdge] = {}
    for edge_type in CODE_GRAPH_EDGE_TYPES:
        for edge in store.get_edges(from_id=node.id, type_=edge_type, limit=100):
            edges[edge.id] = edge
        for edge in store.get_edges(to_id=node.id, type_=edge_type, limit=100):
            edges[edge.id] = edge
    return list(edges.values())


def _code_related_edges_for_nodes(store: GraphStore, nodes: list[MemoryNode]) -> list[MemoryEdge]:
    if not nodes:
        return []
    return store.incident_edges([node.id for node in nodes], edge_types=CODE_GRAPH_EDGE_TYPES, limit=max(10000, len(nodes) * len(CODE_GRAPH_EDGE_TYPES) * 10))


def _archive_code_node(store: GraphStore, node: MemoryNode) -> None:
    properties = dict(node.properties)
    properties["status"] = "archived"
    properties["updated_at"] = utcnow_iso()
    store.update_node_fields(node.id, status="archived", properties=properties)
