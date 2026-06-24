# Architecture

## Goal

REQL implements a storage-agnostic property-graph engine for code memory. The
runtime is deterministic and does not require LLM calls. Optional adapters may
exist at boundaries, but the active graph model is built from repository
structure, parsed code, static analysis, and structural document fragments.

## Layers

```text
Public API
  MemoryGraph

Application Services
  Retrieval / Reporting / Project Scan / Project Compile

Engines
  Activation / Salience / Static Analysis

Storage
  GraphStore / SemanticExtractor / BlockGraphStore

Domain
  MemoryNode / MemoryEdge / Queries / Results / Exceptions
```

The public facade lives in `src/api`. Agent-facing installer integrations live
in `src/agents`. Deterministic graph services live in `src/memory`. The bundled
local graph adapter lives in `memory.storage.adapters`. The MCP transport and tool
handlers live in `src/mcp`.

## Storage Boundary

`memory.storage.GraphStore` is the storage boundary. Services operate on graph
operations such as node/edge upsert, property lookup, bounded neighborhoods,
transactions, and batch writes. The bundled block adapter implements that
contract as a local fixed-size page store; the architecture does not depend on
Neo4j or any external graph service.

Routine operations should prefer bounded or indexed port methods:

- `find_nodes_by_property` and `find_edges_by_property` for project/artifact
  scoped lookups;
- `batch_upsert_nodes` and `batch_upsert_edges` for bulk graph writes;
- `archive_nodes_by_artifact` for artifact deletion handling;
- `bounded_neighborhood` for retrieval and graph exploration.

Full graph loads through `all_nodes` and `all_edges` are reserved for exports,
reports, tests, and explicit administrative inspection.

## Compile Flow

```text
project root
  -> read-only filesystem scan
  -> default ignores plus config include/exclude filtering
  -> dirty planning from .reql/artifact-cache.json fingerprints
  -> register Project, Directory, File, and SourceArtifact deltas
  -> parse dirty code artifacts with Tree-sitter
  -> emit code graph nodes, technical edges, and static-analysis findings
  -> compile document fragments structurally
  -> process document terms, raw events, and co-occurrences locally
  -> link document fragments and ranked terms to high-signal code symbols
  -> archive graph records for deleted artifacts
  -> persist CompilationRun and GraphDelta nodes
```

Code artifacts produce deterministic nodes such as `Module`, `Package`,
`Class`, `Interface`, `Function`, `Method`, `Variable`, `Import`,
`Dependency`, `Endpoint`, `Schema`, `Config`, `Test`, `Comment`, `Docstring`,
and `StaticAnalysisFinding`. Document artifacts produce `SourceFragment`
records, explicit-heading `Concept` nodes, ranked document `Concept` nodes, and
underlying `RawEvent` observations. They are used as source context,
provenance, and deterministic semantic links for the code graph.

Every deterministic compile edge has `confidence=1.0` and provenance fields in
edge properties, including source file, line range, extractor, evidence,
`mode=compile`, `is_semantic=false`, and `is_technical=true`.

## Retrieval

```text
query
  -> deterministic query extraction
  -> lexical seed discovery
  -> bounded graph expansion
  -> graph-aware ranking
  -> subgraph/context output
```

Agent context is built with `reql query_context --query ...`, dependency slices
from `reql query_explore --query ...`, or the structured
`reql query_graph --query ...` command. These builders return bounded graph
context instead of dumping the full store. Lower-level source fragments can
contribute evidence and surrounding text, but query semantics operate over the
higher-level code graph.

## Maintenance

```text
activation and usage signals
  -> salience update
  -> rank useful graph records
  -> keep project/source provenance available for context
```

Salience is code-graph oriented. Legacy conversation-memory maintenance is not
part of the current graph model.

## Analysis

Graph analysis remains deterministic: community detection, hub analysis, and
cleanup findings are graph algorithms with no required LLM or external graph
database. Project compilation stays on the parser and code/document graph path.

`project compile . --watch` is a `watchdog` filesystem monitor over the same
incremental compiler and cache. It uses the same compile pipeline as one-shot
compile, so CLI, API, and MCP updates stay consistent.
