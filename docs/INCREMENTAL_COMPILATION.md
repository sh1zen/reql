# Incremental Compilation

Incremental compilation is project `compile` mode. It builds a deterministic
technical graph for programming agents from compile-time scanning plus AST/static
analysis.

The scanner first runs read-only. The compiler compares fingerprints and
project-local cache entries before graph writes, then registers only
changed/deleted artifact deltas so unchanged project metadata is not rewritten.
The primary cache lives on disk at `<project>/.reql/artifact-cache.json`.
If a project was already compiled but its disk cache or `ArtifactCacheEntry`
nodes are missing, `project compile` recovers cache entries from active compiled
`SourceArtifact` records with matching path, SHA-256, and size instead of
recompiling every file from zero.
The optimization path preserves deterministic graph output: for the same input
and options, the same relevant node types, edge types, properties, provenance,
confidence values, cache records, and `GraphDelta` shape are emitted. Runtime
improvements come from lower-cost storage transactions, batched upserts, cache
planning maps, set-backed delta aggregation, and scoped document-code linking.

## Flow

```text
project compile PATH
project update PATH
  -> scan project read-only
  -> compare artifacts with .reql/artifact-cache.json entries
  -> recover missing cache entries from already compiled SourceArtifact records
  -> register Project, Directory, File, and SourceArtifact deltas for dirty files
  -> compile changed supported code artifacts into technical graph nodes
  -> compile changed text document fragments structurally
  -> link document fragments to code symbols
  -> archive fragments for deleted artifacts
  -> write .reql/artifact-cache.json and ArtifactCacheEntry graph records
  -> write CompilationRun and GraphDelta nodes
```

`project update` and the public `update_project()` API use the same incremental
compile pipeline. They do not maintain a separate update path.

Watch mode wraps the same flow:

```text
project compile PATH --watch
  -> start a recursive Python watchdog observer
  -> run one initial cache check/compile for existing dirty artifacts
  -> wait for filesystem events
  -> wait for debounce when file changes are detected
  -> run project compile PATH only when graph updates are needed
  -> repeat until interrupted or --watch-iterations is reached
```

The watcher uses Python `watchdog` filesystem events. Treat it as monitor mode
for active editing: run it from the agent's working directory, keep one watcher
running, and query the maintained graph instead of launching repeated manual
compile/rebuild loops.

The cache key includes:

- artifact SHA-256;
- file size;
- modification time;
- parser version;
- chunking version;
- options hash.

If all fields match, the artifact is skipped. If any field differs, only that
artifact is recompiled.
When cache entries are absent but the graph already has a compiled
`SourceArtifact` for the same artifact id, relative path, SHA-256, and size,
the compiler treats it as clean and writes a replacement cache entry. Deleted
compiled artifacts are still detected from the project graph in this recovery
path.

Cache planning builds the active cache map for the project once per run from
`.reql/artifact-cache.json`, then merges historical `ArtifactCacheEntry` graph
nodes when needed. The map is reused for clean/changed/deleted/recoverable
decisions, which keeps detection rules unchanged while avoiding repeated
per-artifact graph lookups during cold or dirty compiles.

## Graph Records

`.reql/artifact-cache.json` records the last successful compilation fingerprint
for each project artifact. `ArtifactCacheEntry` nodes mirror those records in
the graph so existing REQL `FIND` statements, reports, and recovery paths remain
available.

`CompilationRun` records one invocation of incremental compilation, including
files seen, changed, skipped, deleted, node and edge update counts, and parser
errors.

`GraphDelta` records the actual graph IDs affected by a run:

- added, updated, and archived nodes;
- added, updated, and archived edges;
- affected node IDs;
- affected community IDs.

During a run the compiler aggregates these IDs in internal sets to avoid
duplicate list growth. The persisted and public `GraphDelta` record still uses
ordered lists with the same field names.

Supported code artifacts produce deterministic technical nodes such as
`Module`, `Package`, `Class`, `Interface`, `Function`, `Method`, meaningful
`Variable`, `Import`, `Dependency`, `Endpoint`, `Schema`, `Config`, and `Test`.
Technical relations include `CONTAINS`, `DEFINES`, `IMPORTS`, `CALLS`,
`REFERENCES`, `INHERITS`, `IMPLEMENTS`, `INSTANTIATES`, `READS`, `WRITES`,
`RETURNS`, `RAISES`, `DECORATED_BY`, `HANDLES_ROUTE`, `TESTS`, `CONFIGURES`,
and `DEPENDS_ON`.

Every deterministic compile edge has `confidence=1.0` and provenance fields in
edge properties: `source_id`, `target_id`, `type`, `confidence`,
`source_file`, `line_start`, `line_end`, `extractor`, `evidence`,
`created_at`, `updated_at`, `mode=compile`, `is_semantic=false`, and
`is_technical=true`.

Node and edge writes are batched where the compiler can safely do so, including
source fragments and cohesive code-graph groups. When storage resolves an
upsert to an existing node through `canonical_key`, subsequent edges use the
persisted node ID returned by the store rather than the candidate ID.

Document-code linking is scoped when only document artifacts changed: only the
dirty document fragments are relinked. If any code artifact changed, the linker
still performs full document-fragment relinking against project code nodes so
existing documentation can link to new or renamed symbols. The linker keeps
only high-signal matches, ignores generic headings and short common terms, and
caps each fragment at 8 code targets.

Unrecognized code-like artifacts are handled conservatively: only safe
file-structure nodes and `CONTAINS` relations are emitted. They do not create
compile errors. The compiler does not invent symbols, calls, imports, source
fragments, topics, communities, or bridges when no normalized project language
is known.

`SourceFragment` nodes are created for supported code symbols and document
artifacts so source-level queries can inspect parsed text. They are not the
primary technical graph for compile mode. Non-code text fragments remain
structural source context for provenance and query context.

## Deletions

When a file disappears from a later scan, its `SourceArtifact` node is archived
along with its `File` node and related `SourceFragment`/code nodes. Cache
entries for deleted artifacts are also archived in the disk cache and graph
cache so subsequent compiles remain idempotent.

## Failure Behavior

Parsing happens before graph writes for each changed artifact. If a parser fails,
that artifact's previous fragments and cache entry are left intact. The
`CompilationRun` is persisted with `failed` status and the error message, and a
run-level `GraphDelta` is still persisted.

Compile writes run inside block-store transactions backed by a per-record
rollback journal. The journal records only touched nodes, edges, pending WAL
state, and small mutable side structures. On rollback, touched records are
restored and indexes are rebuilt from materialized records; on commit, the WAL
semantics remain unchanged. Nested transactions preserve outer work when an
inner transaction rolls back.

## Current Limitations

The compiler creates deterministic source fragments and supported
language-specific code graph nodes. Community impact is reserved for later
phases, so `affected_community_ids` is currently empty unless future services
populate it.


