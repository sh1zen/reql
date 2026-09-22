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

Bulk node reads and neighbor traversal clone records by default for public
callers. Internal read-only compile and retrieval stages may request borrowed
views, but they must clone the final bounded result before crossing the public
API boundary. This avoids repeated deep copies without exposing mutable store
state.

Full graph loads through `all_nodes` and `all_edges` are reserved for exports,
reports, tests, and explicit administrative inspection.

## Compile Flow

Effective configuration is normalized once into the immutable
`CompilationOptions` model before the compile service starts. Scanner policy,
document-format policy, parser selection, watch mode, and cache fingerprinting
all consume that same object. Mapping-based `parsing_options` remain accepted
at the public Python boundary for compatibility, but are not propagated through
the internal pipeline.

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

Every compile phase returns `ArtifactCompilationResult`, whose change sets are
deduplicated at creation time and merged directly. There is no second aggregate
representation with parallel node and edge state.

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

Scoped retrieval starts from bounded lexical posting candidates instead of
enumerating every node in a scope. Matching metrics are computed once and reused
by seed selection and graph expansion; identifier/path components and plural
variants participate in the same deterministic ranking used by `SEARCH`,
`query_context`, `query_graph`, `query_explore`, and `query_memories`.
When the complete query is an indexed project-relative path, retrieval uses the
path property index directly and constrains seeds to that file and source
artifact before expansion. This keeps exact file intent ahead of incidental
prose or shared-extension matches across context, graph, memory, and raw
retrieval surfaces. Documentation-scoped and exact documentation-path queries
use the general/document projection rather than the code-owner projection.
Graph expansion traverses borrowed read-only neighbor views and copies only the
final bounded nodes and edges returned to callers. Ranking, traversal limits,
ordering, and defensive result isolation remain unchanged.

Retrieval modules use explicit imports for their shared constants, helpers, and
domain types. This keeps component dependencies visible and prevents additions
to a common module from silently changing every pipeline stage.

`memory.services.query_context.QueryContextService` is the application boundary
above that pipeline. Python API, CLI, and MCP adapters all submit the same
immutable `QueryContextRequest` and receive a versioned `ContextResult`.
The service owns request-to-`MemoryQuery` conversion, projection, confidence,
trace metadata, deterministic graph revision fingerprinting, and canonical
envelope serialization. Providers do not call retrieval components directly.

`query_context_result` exposes the typed result. Python structured output, CLI
JSON, and MCP serialize schema-v2 envelopes with the query-specific
`graph_revision`, committed `source_revision`, explicit freshness metadata,
`confidence`, and a nested `payload`.

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

## Project Pipeline Projection

`memory.pipeline.ProjectPipelineService` is a second read-only view over the
same compiled graph. Unlike repository explanation, it preserves observed flow
direction: all admitted entrypoints are traversed through project-local calls,
route handling, instantiation, wrappers, and resolved imports. Return, write,
emit, and raise relations become outcomes. Private implementation symbols may
connect the traversal. Test nodes and every node originating from a test path
are removed before adjacency, import, outcome, and component projection, so no
test data can enter the typed pipeline payload or its rendered output.

The service groups reachable symbols with the repository-explanation
capability and layer heuristics. Component nodes are global to the projection,
so multiple workflows converge on the same application, domain, core, or
infrastructure component instead of duplicating it. Aggregated edges retain
their relation and workflow ids; strongly connected components are marked as
feedback cycles. `MemoryGraph.project_pipeline` returns the versioned typed
payload without persisting nodes or metrics. The CLI renders that payload as
Mermaid or as an embedded-data `vis-network` HTML file.

## Agent Operational Memory

`reql agent` is a separate dashboard-centric coordination boundary for agent
sessions, private tasks, private notes, directed notes, and public context. It
does not derive from, copy, query, or synchronize canonical project nodes.

The canonical project graph remains the sole owner of repository identity,
files, symbols, source spans, dependencies, and code relationships. Coding
agents obtain those facts through the normal project query APIs, never through
their operational memory.

The public dashboard is persisted at `.reql/agent-dashboard.reql` and owns the
registered-agent roster, coordination-safe active-task summaries, and shared
context. Shared context records carry a timestamp, agent id, message type, and
content; finish messages, public notes, and task-completion messages are all
records in this section. Each agent's private dashboard remains under
`.reql/agents/` and owns its complete tasks, private notes, external notes, and
session history.

`reql agent init` creates or resumes the selected private dashboard, creates a
session if necessary, and registers the agent as active. `reql agent finish
MESSAGE` closes that session, marks the agent finished, and appends MESSAGE to
public context without deleting private history. `reql agent terminate AGENT_ID`
does the same lifecycle cleanup for stale agents while preserving their tasks
and notes. The dashboard is the persistent coordination layer.

The canonical CLI writes dashboard notes through `agent note TEXT`,
`agent note --agent AGENT_ID TEXT`, and `agent note --public TEXT`.
The dashboard owns cross-agent inspection and public coordination context.
Its private task view renders task ids, lifecycle status, and completion
messages so a later session can resume or audit work without opening raw graph
records. Dashboard search accepts an exact phrase or all normalized query terms
in any order, including across punctuation such as hyphenated identifiers.

## Maintenance

```text
activation and usage signals
  -> salience update
  -> rank useful graph records
  -> keep project/source provenance available for context
```

Salience ranks project and source graph records from structural, retrieval, and
usage signals.

After each successful compilation that creates a changed-manifest
`ProjectRevision`, project-scoped retention keeps the latest
`retention.commits` REQL commits and removes run, delta, revision, archived
graph, and usage-journal data before that commit boundary. It preserves the
active graph and retained commits' associated history, does not advance on
clean or failed compiles, and compacts storage only when graph records were
removed.

## Analysis

Graph analysis remains deterministic: community detection, hub analysis, and
cleanup findings are graph algorithms with no required LLM or external graph
database. Project compilation stays on the parser and code/document graph path.

`project compile --watch` is a `watchdog` filesystem monitor over the same
incremental compiler and cache. It uses the same compile pipeline as one-shot
compile, so CLI, API, and MCP updates stay consistent.
