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

## Main Node Types

- `Project`: scanned project root.
- `Directory`: deterministic project directory.
- `File`: deterministic project file.
- `SourceArtifact`: registered source, document, data, config, or binary file.
- `SourceFragment`: deterministic fragment derived from a source artifact.
- `Module`, `Package`, `Class`, `Interface`, `Function`, `Method`,
  `Variable`, `CodeSymbol`: compile-mode code symbols and containers.
- `Import`, `Dependency`, `Endpoint`, `Schema`, `Config`, `Test`, `Comment`,
  `Docstring`: compile-mode technical graph records.
- `StaticAnalysisFinding`: deterministic static-analysis or cleanup candidate.
- `Concept`: deterministic document term or explicit heading concept.
- `RawEvent`: underlying document observation used as evidence for terms.
- `ArtifactCacheEntry`: successful artifact compilation fingerprint.
- `CompilationRun`: one incremental compilation invocation.
- `GraphDelta`: change trace for a compile/update run.
- `Community`: topological cluster written by deterministic community
  detection.

`Bridge`, `RetrievalTrace`, and related historical memory types remain reserved
constants, but the current compile/reporting pipeline does not persist them.
Retrieval usage is stored in a sidecar usage journal, not as `RetrievalTrace`
nodes or `USED_IN_CONTEXT` edges in the active graph.

## Main Edge Types

The current project compiler, document ingestion, community analysis, and hub
analysis mainly create:

- `MENTIONS`
- `CO_OCCURS_WITH`
- `EVIDENCED_BY`
- `DERIVED_FROM`
- `BELONGS_TO_COMMUNITY`
- `BRIDGES_COMMUNITY`
- `CONTAINS`
- `CONTAINS_FRAGMENT`
- `HAS_SECTION`
- `LINKS_TO`
- `HAS_CODE_BLOCK`
- `DEFINES`
- `METHOD`
- `IMPORTS`
- `DEPENDS_ON`
- `IMPORTS_FROM`
- `RE_EXPORTS`
- `CALLS`
- `REFERENCES`
- `READS`
- `WRITES`
- `RETURNS`
- `RAISES`
- `HAS_DOCSTRING`
- `HAS_COMMENT`
- `INHERITS`
- `IMPLEMENTS`
- `INSTANTIATES`
- `EMITS`
- `DECORATED_BY`
- `HANDLES_ROUTE`
- `HAS_FINDING`
- `TESTS`
- `CONFIGURES`

The domain constants also reserve generic memory edge types such as `ABOUT`,
`SUPPORTS`, `EXPRESSES`, `EXPLAINS`, `SUPERSEDES`, `COMPILED_IN`, `PART_OF`,
`SYNTHESIZES`, `UPDATED_BY`, `TRACKS`, `PARTICIPATES_IN`, `SUPPORTED_BY`, and
`AFFECTED_BY_DELTA`. They are not part of the current project compile/report
output unless a caller or future service explicitly writes them.

Every edge includes weight, confidence, polarity, origin, timestamps, and
additional properties.

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
Function/Method -EMITS-> event symbol
Function/Method -HANDLES_ROUTE-> Endpoint
Test -TESTS-> code symbol
affected code symbol/import -HAS_FINDING-> StaticAnalysisFinding
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
