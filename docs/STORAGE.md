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
  a high-degree node does not make its node page expensive to load;
- schema v2 persists operational indexes in the root index and lazily loads
  node/edge records by location.

The superblock is the durable entry point for validation, recovery, and future
migrations. It records the storage format, manifest version, schema version,
block size, root index offset, data block count, generation id, and a SHA-256
checksum for the data region. Opening a manifested file validates the manifest
checksum first, then validates the data checksum before replaying block records.

For the current format, `data_offset` points to the first data block after the
superblock and `root_index_offset` points to the persisted root-index record
inside the data region.

New checkpoints use the `binary-v2` record codec. Fixed node and edge fields are
encoded as typed binary values; dynamic `properties` and administrative records
remain deterministic JSON payloads inside the binary frame. Payloads are
compressed only when they are large enough and compression reduces size, which
avoids paying zlib CPU for small records. If an encoded logical record is still
larger than one block after compression, the checkpoint writes it as consecutive
record-part frames and reconstructs the original record when the store opens.

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
canonical node keys, edge patterns, incoming/outgoing adjacency, lexical
postings, type/status buckets, selected property indexes, counters, and a
block space map. These indexes are the durable query root for schema v2. Opening
schema v1 files is intentionally rejected.

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

Records are dynamic in size: a small node or edge occupies a small compressed
frame, while larger property payloads take more space without forcing a fixed
record width. Each frame is compressed before being packed into a block, which
keeps more graph records in each page and improves page-cache density.

The default block size intentionally matches common filesystem and cache use
better than many tiny files while remaining simple to inspect and rewrite.

## Inspection And Compaction

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
compaction. It does not delete archived graph records; retention policy remains
a graph-level operation.

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

The default lock timeout is bounded, so a genuinely stuck peer raises
`StorageError` instead of blocking forever. Locks from a different host are
treated as active because process liveness cannot be checked portably across
machines. Read/query CLI commands and read-only MCP tools open the graph
read-only directly, so they do not take the writer lock. Query usage signals
still append to `<store>.usage.jsonl`; that sidecar journal has its own short
exclusive file lock so concurrent readers do not interleave usage writes.
`MemoryGraph.open(..., read_only=True)` requires an existing block store or WAL
payload; it raises `StorageError` instead of returning an empty graph for a
missing or empty storage path.

## Transactions

The adapter exposes `store.transaction()` through the `GraphStore` contract.

- outer transactions snapshot the current in-memory graph and append WAL once
  on commit;
- nested transactions use nested snapshots;
- writes inside failed transactions are rolled back by restoring the relevant
  snapshot;
- `storage compact` is the explicit full-checkpoint path.

Project compilation uses this transaction boundary so a compilation run, graph
updates, cache writes, and graph delta persistence commit atomically.

## Batch And Bounded Operations

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

`all_nodes` and `all_edges` remain available for exports, tests, and
administrative operations where a full graph load is intentional.

