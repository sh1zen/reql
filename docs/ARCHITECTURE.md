# Architecture

## Goal

REQL compiles repository facts into a local property graph and combines bounded
source retrieval with separately stored engineering work. Core compilation,
queries, analysis and storage require no model calls or external graph service.

## Layers

```text
Python API / CLI / MCP
        -> application services
        -> deterministic engines and projections
        -> GraphStore port
        -> local block adapter

Domain models define the records and contracts shared by these layers.
```

| Owner | Responsibility |
| --- | --- |
| `src/api` | Public `MemoryGraph` facade and exported models |
| `src/memory/services` | Compilation, retrieval, coordination and watch orchestration |
| `src/memory/artifacts` | Scanning, compilation, cache, revisions and deltas |
| `src/memory/code_analysis` | Registered Tree-sitter parsers and extractors |
| `src/memory/document_ingestion` / `extraction` | Document parsing and deterministic term processing |
| `src/memory/analysis` / `engines` | Communities, hubs, activation and salience |
| `src/memory/explanation` / `pipeline` | Read-only repository and flow projections |
| `src/memory/storage` | Storage port and block adapter |
| `src/mcp` | Tool handlers and stdio/HTTP transport |
| `src/agents` | Generated assistant guidance, installation and hooks |

## Storage Boundary

`memory.storage.GraphStore` defines graph reads, writes, indexed lookups,
neighborhoods, counts and transactions. Services use this port rather than
backend-specific persistence operations. Adapters implement the complete
[storage contract](EXTENDING.md#storage-adapters).

`memory.storage.adapters.block_store.BlockGraphStore` owns block persistence,
transaction journals, lazy loading and lexical-index lifecycle. Stateless term
selection lives in `memory.storage.adapters.lexical_index`. Package exports
resolve to the same store class.

Routine queries use indexed or bounded operations. Public results remain
independent of mutable store state; internal read-only stages may borrow records
and copy their final bounded results. Full graph reads are reserved for exports,
reports and explicit administration. See [Storage](STORAGE.md) for format,
locking, transaction and borrowed-read details.

## Compile Flow

```text
effective configuration -> CompilationOptions
project files -> verified scan -> dirty planning
dirty artifacts -> parsing and graph writes -> links and findings
deletions -> archival and cache invalidation
run / delta / revision -> disk-cache persistence -> retention and checkpoint
```

The configuration owner produces one immutable `CompilationOptions` model.
Scanner policy, parser selection, document policy, watch mode and cache
fingerprinting consume it. The scanner owns filesystem hashes and artifact
identities; the compiler and cache own parser versions and compilation provenance.

Each artifact phase returns `ArtifactCompilationResult`, with set-backed change
collections merged by the orchestration service. Code and documents share source
artifacts and fragments while retaining distinct parser results and graph facts.
Technical edges retain deterministic confidence and source provenance.

`IncrementalCompilationService` owns stage ordering and transaction boundaries.
`ProjectRegistry` registers project structure; `ArtifactCompiler` parses and
persists artifact facts; cache and revision repositories own successful-content
provenance. `update_project` and watch mode reuse this pipeline. See
[Incremental compilation](INCREMENTAL_COMPILATION.md) for the complete flow,
cache recovery, partial failures and deletion behavior.

## Retrieval

```text
QueryContextRequest -> QueryContextService -> RetrievalEngine
  -> lexical seeds -> bounded expansion -> ranking
  -> code / general / cleanup projection -> Markdown or structured output
```

`memory.services.query_context.QueryContextService` is the common Python, CLI
and MCP boundary. It owns request conversion, confidence, revision and freshness
metadata, engineering-context attachment, and `ContextResult` serialization.
Providers submit typed requests rather than calling retrieval components directly.

Within `memory.services.retrieval`:

- `search.py` selects and scores lexical candidates;
- `expansion.py` traverses bounded neighborhoods;
- `context/service.py` coordinates projection;
- `context/projections/` selects code, general and cleanup evidence;
- `context/renderers/` formats that evidence;
- `context/models.py` defines shared internal contracts.

Exact indexed paths use property lookups before graph expansion. Other searches
share identifier/path tokenization and deterministic ranking. Source-backed code
context includes owners, test targets and existing implementation/caller links;
static links require verification against current source.

`retrieval.context.source` owns bounded file capture for previews and inspection.
It confines paths to the registered project and verifies successfully compiled
hashes against committed revisions. Request-local snapshots avoid repeated reads.
Stale verified-source requests return reasons rather than indexed bodies.
See [CLI retrieval](CLI.md#retrieval-commands) for source budgets, statuses,
inspection commands and schema-version-2 envelopes.

## Repository Explanation

`RepositoryExplanationService` projects compiled code into capabilities,
architectural layers, corroborated semantic workflows and change starting points.
`MemoryGraph.explain_project` and `project explain` compute this view without
persisting inferred entities or changing graph metrics. Owners and workflow
participants retain graph ids and source locations. See
[Repository explanation](REPOSITORY_EXPLANATION.md).

## Project Pipeline Projection

`ProjectPipelineService` follows project-local entrypoints through observed calls,
handlers, instantiation, wrappers and resolved imports. Writes, returns, emissions
and raises become outcomes. Test-originating records are excluded; private
implementation symbols can connect observed paths.

The view groups reachable symbols into shared architectural components and marks
feedback cycles. `MemoryGraph.project_pipeline` returns a typed projection without
persisting it; the CLI renders HTML or Mermaid. See
[pipeline export](CLI.md#project-pipeline-export) for selection and output behavior.

## Engineering Work and Agent Sessions

Three owners keep source facts and operational state separate:

| State | Owner and location |
| --- | --- |
| Repository facts | Canonical project graph, normally `.reql/memory.reql` |
| Shared engineering work | `CoordinationStore`, `.reql/agent-dashboard.reql` |
| Private session scratch | `AgentWorkspace`, `.reql/agents/` |

`CoordinationStore` validates work records and relations, checks revisions and
performs atomic single-record updates. Read projections derive readiness,
conflicts, replacements and project direction. Batches call that same owner in
order and preserve committed receipts on failure; a batch is not one transaction.

`AgentWorkspace` supplies identity and session provenance. Finish saves a shared
checkpoint before closing the session and releasing private scratch. Shared work
survives session cleanup and contains evidence paths rather than copied source
records. Lifecycle locks and storage leases protect concurrent access.

`QueryContextService` joins bounded work evidence to source context without
changing source confidence or revision semantics; work has its own revision.
`project context` reads work without opening the canonical graph, while
`project overview` combines source architecture and operational projections.
Its `--details` mode can additionally read available private session stores.
Text rendering keeps response-local references without mutating stored records.
See [Engineering coordination](COORDINATION.md) for schema, budgets, ownership,
handoffs, retention and revision rules.

`src/agents/gen-skill.py` is the source for installed skills and platform rules.
`src/agents/install.py` owns installation and guidance hooks. These integrations
route agents to existing commands; they do not infer intent from transcripts.

## Maintenance

Successful compilations that change the project manifest create REQL commits.
Project retention prunes eligible history while preserving active facts and
retained commits. Agent-session and shared-work retention have separate owners;
see [Configuration](CONFIGURATION.md) and [Storage maintenance](STORAGE.md#inspection-compaction-and-clean-rebuilds).

Watch mode wraps the same incremental compiler. One watcher lease owns refresh;
the graph is opened for compile batches and released while idle. The shared
event handler excludes owned storage and diagnostics output while retaining
source edits and moves. See [watch flow](INCREMENTAL_COMPILATION.md#flow).

## Analysis

Activation and salience rank graph records using structural and usage signals.
Community and hub analysis can persist their results through the storage port;
explanation and pipeline views remain read-only. See
[Graph analysis](GRAPH_ANALYSIS.md) for formulas, outputs and limits.
