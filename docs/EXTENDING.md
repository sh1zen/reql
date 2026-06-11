# Extending

## Storage adapters

Implement `GraphStore` in `memory.ports.graph_store`.

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

Implement `SemanticExtractor` in `memory.ports.extractor`.

The extractor should return `ExtractionResult` objects. Core compile paths use
deterministic local processing. Document processing lives in
`memory.extraction.document_processor` and emits ranked terms, raw events,
co-occurrence relations, and links to code symbols.

## New engines

New engines should be placed under `engines/` if they operate on low-level memory state, for example:

- reward engine;
- centrality engine;
- vector index engine;
- temporal reasoning engine;
- privacy/redaction engine.

Application orchestration should remain under `services/`.

## New node or edge types

Add constants in `domain/constants.py`, then update services/engines as needed. The storage layer accepts arbitrary `type` strings, so schema evolution is mainly a domain concern.


