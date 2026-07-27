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
  -> QueryContextRequest
  -> QueryContextService
  -> deterministic query extraction
  -> lexical seed discovery
  -> bounded graph expansion
  -> graph-aware ranking
  -> code/general/cleanup projection
  -> Markdown or structured context output
```

`memory.services.retrieval.RetrievalEngine` remains the stable facade, but its
implementation is assembled from focused pipeline components:

- `retrieval/search.py` owns lexical matching and ranking primitives;
- `retrieval/expansion.py` owns bounded graph traversal;
- `retrieval/context/service.py` coordinates context construction;
- `retrieval/context/projections/` selects code, general, and cleanup payloads;
- `retrieval/context/renderers/` turns payloads into Markdown or structured
  dictionaries;
- `retrieval/context/models.py` defines the internal models and component
  protocols shared by the pipeline.

`memory.services.query_context.QueryContextService` is the application boundary
above that pipeline. Python API, CLI, and MCP adapters all submit the same
immutable `QueryContextRequest` and receive a versioned `ContextResult`.
The service owns request-to-`MemoryQuery` conversion, projection, confidence,
trace metadata, deterministic graph revision fingerprinting, and canonical
envelope serialization. Providers do not call retrieval components directly.

`query_context_result` exposes the typed result. Python structured output, CLI
JSON, and MCP all serialize it as the same envelope with `schema_version`,
`graph_revision`, `confidence`, and a nested `payload`.

Agent context is built with `reql query_context --query ...`, dependency slices
from `reql query_explore --query ...`, or the structured
`reql query_graph --query ...` command. These builders return bounded graph
context instead of dumping the full store. Lower-level source fragments can
contribute evidence and surrounding text, but query semantics operate over the
higher-level code graph.

## Repository Explanation

`memory.explanation.RepositoryExplanationService` is a read-only projection
over the compiled technical graph. It groups modules and high-signal symbols
into business capabilities, assigns architectural roles, builds semantic
workflow entities from corroborating call, dependency, structure, convention,
signature, and documentation evidence, and ranks code starting points for an
optional focus phrase.

The projection is computed on demand by `MemoryGraph.explain_project` and
`reql project explain`. It does not persist inferred capability or workflow
nodes, does not mutate graph metrics, and does not require an LLM. Every owner,
workflow participant, workflow evidence item, and change starting point retains
a node id and source location. Workflow participants are exposed through
`implemented_by` relations rather than an invented linear call path. This keeps
the business view explainable while allowing the underlying code graph to
remain the single source of truth.

## Maintenance

```text
activation and usage signals
  -> salience update
  -> rank useful graph records
  -> keep project/source provenance available for context
```

Salience ranks project and source graph records from structural, retrieval, and
usage signals.

## Analysis

Graph analysis remains deterministic: community detection, hub analysis, and
cleanup findings are graph algorithms with no required LLM or external graph
database. Project compilation stays on the parser and code/document graph path.

`project compile . --watch` is a `watchdog` filesystem monitor over the same
incremental compiler and cache. It uses the same compile pipeline as one-shot
compile, so CLI, API, and MCP updates stay consistent.
