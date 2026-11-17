# Incremental Compilation

`project compile`, `MemoryGraph.update_project` and watch mode use the same
`IncrementalCompilationService`. The scanner reads files without changing them,
hashes their full contents, and verifies size and nanosecond modification time
before and after each read. Unstable files are retried, then reported as scan
errors. Hashing uses a direct path for zero/one file, two workers for small scans
and at most eight workers for larger scans, bounded by available CPUs.

The compiler registers changed structure and reconciles deletions rather than
rewriting unchanged project/file metadata. Effective policy is one immutable
`CompilationOptions` model; see [Configuration](CONFIGURATION.md#loader).
Code and document parsing are described in [Code analysis](CODE_ANALYSIS.md)
and [Artifact ingestion](ARTIFACT_INGESTION.md).

Parser loading remains offline: missing language-pack grammars yield skipped
module metadata and setup guidance, without implicit downloads. See
[Tree-sitter dependencies](CODE_ANALYSIS.md#tree-sitter-dependencies).
Skipped code-parser results cannot establish a reusable cache entry; the next
compilation retries them, including after explicit grammar installation.
C syntax recovery can retain intact function definitions beneath preprocessor
errors while preserving partial-parser diagnostics. It does not treat damaged
declarators/bodies as reliable evidence. The parser/compiler fingerprints advance
when these extraction rules change, causing one recompilation of older snapshots.

## Flow

```text
project compile
  -> open and validate graph storage
  -> scan and hash project files
  -> plan changed, deleted and recoverable artifacts
  -> graph transaction:
       register changed structure and compile artifacts
       derive structural/document links and findings
       archive deleted artifacts and invalidate their cache entries
       persist CompilationRun and GraphDelta
       record ProjectRevision when the run succeeds and its manifest changes
  -> persist new successful disk-cache entries
  -> prune eligible history after a new successful revision
  -> checkpoint storage when needed
```

Each artifact compiles inside a nested graph transaction. An artifact exception
rolls back its writes while other artifacts can still succeed. Project-wide
structural/unused/orphan analysis runs only at the stages whose error guards
allow it; a failed run does not publish a committed project revision.

Document-only changes relink their dirty fragments. A code change relinks
document fragments against the updated project symbol set. CSS surface findings
refresh once after changed/deleted artifacts have been processed.

New successful cache entries are written as one disk batch **after** the graph
transaction commits. Deletion handling invalidates existing entries during
archival. The graph and JSON cache are separate persistence boundaries; completed
artifact provenance permits recovery when a cache entry is missing. Mismatched
existing entries require recompilation.
See [Storage transactions](STORAGE.md#transactions).

Every run that reaches transaction persistence records a `CompilationRun` and
`GraphDelta`, including clean and artifact-failure runs. A scan failure aborts
before artifact registration and run persistence. CLI counters report graph
upserts, while the changed-symbol summary excludes equivalent symbols and
line-only movement; see [Project compilation](CLI.md#project-compilation).

Watch mode performs an initial verified compile, then processes source events
through the same scanner and dirty planner. One watcher lease owns a project;
the canonical graph is opened for compile batches and released while idle.
`project watch-status` reads watcher state without opening the graph.

The shared handler excludes storage, WAL, lock, cache, usage and diagnostics
sidecars. Read-only open/close and redundant parent-directory notifications do
not trigger compilation; writes, creates, deletes and moves do. Moves across
an ignored boundary still report the affected source path. See
[watch commands and limits](CLI.md#incremental-compilation) and
[storage locking](STORAGE.md#readerwriter-locking).

Cache entries compare:

- artifact SHA-256, size and modification time;
- parser and chunking versions;
- effective compilation-options hash and active status.

Both cache hits and recovery also require a matching successfully compiled
digest and compiler-contract fingerprint on the active `SourceArtifact`.
Scan-time metadata alone cannot validate compiled spans or failed work.

The primary cache is `<project>/.reql/artifact-cache.json`. Planning builds one
active project map from disk and graph cache records. Missing entries can be
recovered from active compiled artifacts with matching identity, path, content
and compiler provenance. Clearing the cache therefore does not necessarily
recompile unchanged artifacts or reset the graph.

`cache_enabled=False` disables reuse and new successful-entry writes. It still
reconciles deletions and invalidates their existing cache entries. Changing
options without cache writes cannot make an earlier cache entry valid for a
different graph snapshot.

Context-scope metadata is written by artifact/code/document producers. Clean
artifacts are not revisited to repair missing fields; recompile an incomplete
artifact so its producer writes the canonical representation.

## Graph Records

| Record | Meaning |
| --- | --- |
| `SourceArtifact` | Registered path/content metadata and successful compilation provenance |
| `ArtifactCacheEntry` | Successful fingerprint mirrored for queries, reports and recovery |
| `CompilationRun` | Invocation status, artifact counts, graph-write counts and errors |
| `GraphDelta` | Added, updated and archived graph ids, plus affected nodes/communities |
| `ProjectRevision` | Content-addressed manifest, parent revision and changed file paths |

Change collections use sets internally and ordered lists at persistence/public
boundaries. Edges use the persisted ids returned by upserts, including when a
canonical key resolves to an existing node. See [Schema](SCHEMA.md) for record
fields and [Code analysis](CODE_ANALYSIS.md#extracted-graph) for technical output.

A successful changed manifest is a REQL commit. Clean and failed runs do not
advance the commit-retention window. Project history, retained run/delta records,
archived graph records and usage events follow the
[configured retention policy](CONFIGURATION.md#behavior); physical cleanup is
described in [Storage maintenance](STORAGE.md#inspection-compaction-and-clean-rebuilds).

## Deletions

A later scan archives missing `SourceArtifact` and `File` nodes with their
related fragments/code records. Deleted cache entries are invalidated in disk
and graph metadata so later compiles remain idempotent. This also applies with
cache reuse disabled and when an artifact's first compilation failed.
An incomplete scan cannot establish deletions.

## Failure Behavior

Unreadable directories/files or repeatedly changing scan content raise
`StorageError` with affected paths before compilation writes. Existing committed
graph facts and cache entries remain available; resolve the error and retry.

An artifact exception preserves that artifact's prior graph facts and skips its
cache upsert. Returned parser errors also mark the run failed. Other successful
artifacts and run/delta records can commit; a failed run is not a whole-project
rollback and does not create a project revision. Source verification still
requires committed revision provenance.

If new disk-cache persistence fails after graph commit, the committed graph is
not rolled back. After repairing cache storage, subsequent compilations validate
entries against successful graph provenance, using graph cache records or
recompiling where needed. Nested rollback and WAL behavior belong to the
[storage transaction contract](STORAGE.md#transactions).

## Current Limitations

Compilation does not recompute communities; `affected_community_ids` is currently
empty. Invoke graph analysis explicitly when its persisted results need updating.
