# Storage

REQL uses a storage-agnostic `memory.storage.GraphStore` contract. The bundled local adapter is
`memory.storage.adapters.BlockGraphStore`, re-exported as
`memory.storage.BlockGraphStore`, a dependency-light graph store that writes
fixed-size block files directly instead of using a database engine.

## Block Adapter

`BlockGraphStore` stores graph data in REQL block files:

- block 0 is a superblock manifest;
- every disk block has a fixed size, 64 KiB by default;
- each block maps cleanly to the in-process page cache;
- records are length-prefixed binary payloads with selective compression;
- node records are packed near ordinary incident relationships to preserve
  graph-neighborhood locality;
- relationships for dense nodes are written as dedicated dense-edge records so
  a high-degree node does not make its node page expensive to load.

The superblock is the durable entry point for validation and recovery. It
records the storage format, manifest version, schema version,
block size, root index offset, data block count, generation id, and a SHA-256
checksum for the data region. Opening a manifested file validates the manifest
checksum first, then validates the data checksum before replaying block records.
The manifest requires an explicit `checksum_algorithm: "sha256"`. Version,
size, offset, block-count and generation fields must be JSON integers; strings,
booleans and floating-point values are rejected. Incomplete manifests require
a rebuild with the current REQL version. Storage opens require store schema
2, root-index version 3, lexical schema 2 and the `binary-v2` record codec.
Writable, read-only and deferred opens use the same format validation; errors
leave the checkpoint untouched.

For the current format, `data_offset` points to the first data block after the
superblock and `root_index_offset` points to the persisted root-index record
inside the data region.

Record encoding, compression and multi-block records are described under
[Compression and dynamic records](#compression-and-dynamic-records).

Writable sessions append canonical graph changes to a sidecar WAL,
`<store>.wal`, after the initial checkpoint exists. Opening a store validates
the checkpoint, loads the persisted root index, replays the WAL if present, and
avoids materializing graph records until an indexed lookup needs them. WAL replay
coalesces repeated node/edge frames before updating indexes, and lexical
reindexing is bounded per node so a WAL-only recovery does not become quadratic
on large source fragments. `close()` does not rewrite the full graph; it only
ensures pending WAL frames are durable. Compile/update and normal CLI opens
checkpoint automatically when the WAL is large or the base checkpoint is
missing, so `reql storage compact` is maintenance, not a required post-compile
step. Checkpoint flushes reuse encoded records across root-index stabilization
passes and stream data pages directly to disk, avoiding repeated compression and
full-file memory copies.

Query usage signals are stored separately in `<store>.usage.jsonl`. Retrieval
can update usage while the graph is opened read-only, and ranking loads that
overlay on open without rewriting canonical node or edge records.

Each checkpoint also stores a `root_index` record. It captures record locations,
canonical node keys, edge patterns, incoming/outgoing adjacency, type/status
buckets, selected property indexes, counters, and a block space map. Lexical
postings live in a separate `lexical_index` record referenced by root-index
version 3. Read-only CLI/MCP sessions load the lexical record on first search;
metadata reads need only structural indexes. Compilation sessions defer the
lexical record and append changed nodes to the WAL, so a small graph delta does
not pay the full lexical-index startup cost. This also applies to a newly created
store. Repeated updates to the same node are coalesced before the first query or
checkpoint builds the postings. The deferred session tracks an exact
old/new lexical overlay: an in-process query applies it before searching, rollback
restores it transactionally, and an automatic checkpoint loads and folds it into
the persisted postings once the WAL threshold is reached. Deferral therefore
does not create stale same-process queries or an indefinitely growing WAL.

Loading the split lexical record builds both term-to-node and node-to-term indexes
together. Neither direction is published after a conversion failure; pending edits
remain available for retry.

`top_nodes_by_degree` preserves defensive copies by default and copies only the
selected rows after ranking. Internal read-only projections may pass
`clone=False`, matching other borrowed store reads; borrowed nodes must not be
mutated or returned as public mutable state.

The adapter keeps operational indexes available for deterministic graph access:

- node id;
- `(type, canonical_key)`;
- edge id;
- `(from_id, to_id, type)`;
- incoming and outgoing adjacency lists;
- deterministic lexical terms by term and node;
- type/status buckets;
- selected scalar property indexes for artifact, cache, source, and project
  query fields.

This keeps retrieval, activation, cache inspection, project compilation, and
common REQL query paths bounded without relying on SQL or a graph database
service.

The block adapter owns one lexical-index implementation, using stateless term
selection helpers in `memory.storage.adapters.lexical_index`. Import the store
from `memory.storage`, `memory.storage.adapters`, or the public `reql` package.
See [clean rebuilds](#inspection-compaction-and-clean-rebuilds) for explicit
project storage maintenance.

Indexing and retrieval share immutable short-text analysis. Its process-local
cache retains at most 1,024 strings of up to 2,048 characters; larger text uses
the same uncached algorithm. Public token lists remain independent of cached values.

Lexical postings include bounded identifier components (camelCase, snake_case,
hyphenated names, and paths) plus conservative singular variants. Bounded
searches rank posting scores before materializing records and only load a small
candidate pool; unbounded callers retain the complete-result behavior.

## Data Locality

During flush, the store writes node records ordered by type, creation time, and
id. For ordinary nodes, incident edges are emitted immediately after
the node when possible. This means loading a block usually brings a useful
neighborhood into memory together: the node, its properties, and nearby
relationships.

Dense nodes are detected by degree. When a node's incident relationship count
is at or above the dense-node threshold, its edges are emitted in dense-edge
records after the node section. The node remains small and cheap to load, while
its large relationship set remains indexed and queryable through adjacency
indexes.

## Compression And Dynamic Records

Fixed node and edge fields are
encoded as typed binary values; dynamic `properties` and administrative records
remain deterministic JSON payloads inside the binary frame. Payloads are
compressed only when they are large enough and compression reduces size, which
avoids paying zlib CPU for small records. If an encoded logical record is still
larger than one block after compression, the checkpoint writes it as consecutive
record-part frames and reconstructs the original record when the store opens.

## Inspection, Compaction, And Clean Rebuilds

Use `reql storage inspect` to print block-level diagnostics for the selected
storage file. The report includes block count, record count by kind,
compressed and uncompressed payload bytes, compression ratio, dense-node count,
manifest metadata, WAL status, root-index counts, space-map free bytes, indexed
record counts, and loaded cache counts. Pass `--json` for a structured payload
suitable for monitoring or regression tests.

Use `reql storage compact` after large maintenance runs or many archive
operations when you want an explicit storage rewrite or to reclaim checkpoint
space immediately. The graph is already query-ready before compaction because
automatic checkpoints and bounded WAL replay are part of normal operation. The
command reloads the logical graph, writes a fresh compact generation, and
reports generation id, block count, record count, and byte size before and after
compaction. It does not itself delete archived graph records. Successful
compilation operations that create a changed-manifest `ProjectRevision`
enforce the graph-level `retention.commits` policy and invoke compaction only
when they actually remove graph records.
History selection applies limits after ordering by committed revision sequence
or delta creation time. Retention boundaries use committed history timestamps;
later activation, usage, or analysis updates cannot change the retained revisions.

Use `reql storage clear` when archived records, compilation history, and
other state that no longer belongs to the current project tree must be removed.
REQL compiles `PATH` into a temporary block store while holding the destination
writer lock, compacts and validates that clean generation, and atomically
replaces the old store only after a successful compile. The artifact cache is
rebuilt in the same maintenance window; failures restore the previous cache and
leave the old store untouched. A successful clear also removes the old WAL and
usage journal.

## Agent Memory Isolation

Private stores under `.reql/agents/` and the shared `agent-dashboard.reql` use
the block adapter but contain operational records rather than project graph
replicas. Agent initialization and shared-work retrieval do not open the
canonical graph. Lifecycle cleanup locks private stores and defers busy stores;
the last active session releases shared private scratch.

See [Engineering coordination](COORDINATION.md) for checkpoint creation,
session/public-message retention and independent shared-work retention.

## Reader/Writer Locking

`BlockGraphStore` uses a cross-platform reader/writer lock before opening a
store. Writable opens acquire an atomic sidecar writer file named
`<store>.lock`, then wait for active read slots in `<store>.readers/` to drain.
Read-only opens create one atomic reader slot and proceed concurrently with
other readers as long as no writer is active or waiting.

This gives local database-like isolation for parallel Codex, MCP, CLI, or user
processes:

- multiple read-only commands can run at the same time;
- a writer excludes other writers and new readers;
- a writer waits for existing readers before opening;
- a reader waits for or rejects an active writer according to the configured
  lock timeout;
- stale same-host lock files from dead processes are removed automatically.

Writer and reader-slot release retry Windows sharing violations for up to one
second, allowing brief metadata-reader handles to close. Each retry rechecks the
ownership token before deleting the sidecar. Other deletion errors and persistent
sharing violations still propagate to the caller. If an owner releases between
an acquisition check and its metadata read, the contender retries acquisition;
timeout diagnostics also tolerate vanished sidecars.
Windows may report access denied while a deleted sidecar is pending removal;
exclusive creation retries that lock-file error within the acquisition timeout.
Reader-slot creation likewise tolerates concurrent removal of an empty slot
directory. Persistent access failures still report the original cause.

Lock sidecars record the owning command, PID, host, creation time, and whether
the command is a `--watch` process. Inspect them without opening the graph:

```bash
reql storage locks
reql storage locks --json
reql storage locks --recover-stale
```

Diagnostics report lock duration and same-host process liveness. Recovery only
removes a lock when its local PID is no longer alive; an incomplete or malformed
local lock must also exceed a safety grace period, and a different-host lock is
never removed automatically.

The default lock timeout is bounded, so a genuinely stuck peer raises
`StorageError` instead of blocking forever. Locks from a different host are
treated as active because process liveness cannot be checked portably across
machines. Read/query CLI commands, including `project status`, `query_context`,
and non-mutating `query` statements, plus read-only MCP tools open the graph
read-only directly, so they do not take the writer lock. Query usage signals
still append to `<store>.usage.jsonl`; that sidecar journal has its own short
exclusive file lock so concurrent readers do not interleave usage writes.
`MemoryGraph.open(..., read_only=True)` requires an existing block store or WAL
payload; it raises `StorageError` instead of returning an empty graph for a
missing or empty storage path.

When a writer is active, read commands respect its lock and either wait or
raise `StorageError` according to their configured lock timeout.

## Transactions

The adapter exposes `store.transaction()` through the `GraphStore` contract.

- each transaction journals touched nodes, edges, pending WAL state, and small
  mutable side structures;
- inner rollback restores its journal while preserving outer work;
- rollback restores touched records and rebuilds indexes, including deferred
  lexical postings;
- the outer commit appends pending WAL records once;
- `storage compact` is the explicit full-checkpoint path.

Project graph updates, cache-entry nodes and run/delta persistence share this
transaction boundary. The JSON artifact cache is separate: new successful
entries persist after graph commit, while deletion handling invalidates existing
entries during archival. Matching compiled provenance supports cache recovery;
see [Incremental compilation](INCREMENTAL_COMPILATION.md#failure-behavior).

## Batch And Bounded Operations

Bulk reads and neighbor traversal clone records by default. Internal stages
may request borrowed views but must not mutate them or return them as public
mutable state. Final bounded public results are copied.

The port includes helpers for storage-efficient operations:

- `batch_upsert_nodes`;
- `batch_upsert_edges`;
- `find_nodes_by_property`;
- `find_edges_by_property`;
- `incident_edges`;
- `count_nodes`;
- `count_edges`;
- `node_type_counts`;
- `top_nodes_by_degree`;
- `archive_nodes_by_artifact`;
- `bounded_neighborhood`;
- `update_node_metrics`;
- `persist_analysis_results`.

These methods keep routine project compilation, artifact cache inspection, and
activation/retrieval operations bounded instead of loading the whole graph.
Query, analysis, and reporting paths should prefer these APIs for pagination,
counts, and ranked candidates; `all_nodes` and `all_edges` should be reserved
for intentional full exports or explicit administrative scans.
