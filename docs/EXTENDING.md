# Extending

## Storage adapters

Implement `GraphStore` from `memory.storage.graph_store`.

A backend must support:

- node upsert;
- edge upsert;
- id and canonical-key lookup;
- adjacency queries;
- lexical seed search;
- degree calculation;
- export.

The service layer should not assume any database-engine-specific behaviour.

## Extractors

Implement `SemanticExtractor` from `memory.storage.extractor`.

The extractor should return `ExtractionResult` objects. Core compile paths use
deterministic local processing. Document processing lives in
`memory.extraction.document_processor` and emits ranked terms, raw events,
co-occurrence relations, and links to code symbols.

## Document parsers

Add document format parsers under `memory/document_ingestion/formats/` and
inherit `BaseDocumentParser` from `memory.document_ingestion.base`. The common
base handles format support checks, UTF-8 decoding with replacement, file
metadata, and path-derived titles. Register new parsers in
`default_parser_registry()` when they should participate in project
compilation. Keep optional format dependencies graceful, following the PDF
parser pattern.

## New engines

New engines should be placed under `memory/engines/` when they operate on
low-level graph state in the same style as the current activation and salience
engines. Keep project orchestration under `memory/services/`, and keep graph
analysis algorithms under `memory/analysis/`.

Existing examples:

- `memory.engines.activation.ActivationEngine`
- `memory.engines.salience.SalienceEngine`
- `memory.analysis.communities.CommunityDetector`
- `memory.analysis.hubs.HubAnalyzer`

## New node or edge types

Add constants in `memory/domain/constants.py`, then update compiler, retrieval,
reporting, services, engines, or analysis code only where the new type needs
dedicated behavior. The storage layer accepts arbitrary `type` strings, so
schema evolution is mainly a domain and service concern.


