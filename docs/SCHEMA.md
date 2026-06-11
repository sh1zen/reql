# Schema

REQL's active schema is a code graph. Nodes describe projects, source
artifacts, source fragments, code symbols, deterministic document terms and
events, static-analysis findings, graph-analysis records, cache entries,
compilation runs, and deltas. Edges describe containment, definitions,
references, calls, imports, inheritance, configuration, tests, provenance,
document co-occurrence, cleanup findings, and graph-analysis membership.

## Node Records

Each `MemoryNode` has:

- `id`
- `type`
- `label`
- `text`
- `canonical_key`
- `properties`
- status
- salience, activation, confidence
- evidence and usage counters
- timestamps

Free-search retrieval ranks active records with lexical coverage and graph
context. It does not use conversation-memory metrics such as volatility,
utility, or contradiction signals.

## Edge Records

Each `MemoryEdge` has:

- `id`
- `from_id`
- `to_id`
- `type`
- weight
- confidence
- polarity
- origin
- `properties`
- timestamps

Edges are directed. `from_id` is the outgoing/source endpoint and `to_id` is
the incoming/target endpoint. Query and context payloads may expose aliases such
as `source_id`, `target_id`, `incoming`, and `outgoing`, but the stored
canonical edge record remains `from_id -> to_id`.

## Main Code Types

- `Project`, `Directory`, `File`, `SourceArtifact`, `SourceFragment`
- `Module`, `Package`, `Class`, `Interface`, `Function`, `Method`, `Variable`
- `Import`, `Dependency`, `Endpoint`, `Schema`, `Config`, `Test`
- `Concept`, `RawEvent`
- `Comment`, `Docstring`, `StaticAnalysisFinding`
- `ArtifactCacheEntry`, `CompilationRun`, `GraphDelta`
- `Community`, `Bridge`

Retrieval usage is stored in a sidecar usage journal, not as `RetrievalTrace`
nodes or `USED_IN_CONTEXT` edges in the active graph.

## Main Relations

```text
Project/Directory/File/SourceArtifact -CONTAINS-> child artifact or fragment
SourceArtifact -DEFINES-> Module/Class/Function/Method/etc.
Class/Interface -METHOD-> Method
SourceFragment -REFERENCES-> code symbol
Function/Method -CALLS-> Function/Method
File/Module -IMPORTS-> Import
File -DEPENDS_ON-> Dependency
Module -IMPORTS_FROM-> Dependency
Module -RE_EXPORTS-> Import
Class -INHERITS-> Class
Class -IMPLEMENTS-> Interface
Function/Method -READS/WRITES/RETURNS/RAISES-> code node
Function/Method -INSTANTIATES-> Class/Interface
Function/Method -HANDLES_ROUTE-> Endpoint
Test -TESTS-> code symbol
affected code symbol/import -HAS_FINDING-> StaticAnalysisFinding
GraphDelta -AFFECTED_BY_DELTA-> changed graph record
graph member -BELONGS_TO_COMMUNITY-> Community
bridging node -BRIDGES_COMMUNITY-> neighboring Community
Concept -CO_OCCURS_WITH-> Concept
```

## Document Relations

Project compilation records deterministic document structure and code-derived
links. Markdown, text, and PDF inputs create structural `SourceFragment` records
with source location metadata. Non-code documents remain source context for the
code graph.

Code compilation remains local/static: unresolved low-signal call targets are
summarized on the owning symbol instead of materialized as standalone nodes.
Unused-code cleanup candidates are materialized as `StaticAnalysisFinding`
nodes with `evidence_scope`, `confidence`, `cleanup_priority`, and numeric
`cleanup_rank`.
