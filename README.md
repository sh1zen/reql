# REQL

REQL is a local repository context and working-memory layer for coding agents
and developer tools. It compiles source files and supported documents into a
property graph, then answers bounded queries over code, symbols, tests,
documents, dependencies, findings, and provenance.

In the intended coding-agent integration, the user does not treat REQL as a
separate manual workflow. After the assistant instructions or skill are
installed for Codex, Claude, Gemini, Cursor, or another agent environment, the
agent uses REQL while it works: it compiles or refreshes the repository graph,
retrieves compact source-backed context, records durable goals, outcomes and
decisions, and reconstructs operational history and plans after context loss.
Repository facts remain exclusively in the canonical project graph.

**Token and reasoning budget:** REQL helps coding agents spend fewer tokens on
repository discovery and more tokens on the actual change. Bounded retrieval
returns the files, symbols, relationships, and source spans that matter for the
current task, while Agent Workspace preserves the task map, decisions, risks,
and finish messages needed to reason through complex or large implementations across
context windows.

The important part is that REQL gives the agent deterministic repository memory
before and during edits:

- `project compile` scans the project, fingerprints artifacts, parses supported
  code and documents, and writes graph nodes, edges, cache records, compilation
  runs, and deltas;
- application surface files are linked deterministically when the graph can
  infer relationships, such as controller render calls to templates and shared
  template/CSS/JS identifiers;
- retrieval commands such as `query_context`, `query_explore`, `query_graph`,
  and `query_memories` find lexical seed nodes, expand a bounded graph
  neighborhood, rank the result, and return compact source-backed context;
- Python, CLI, and MCP `query_context` calls share one typed request/result
  service, so scopes, budgets, confidence, and revision metadata have identical
  semantics at every provider boundary;
- every result can point back to paths, line ranges, relationships, evidence,
  and graph provenance instead of relying on broad source dumps;
- `reql agent` maintains shared structured goals, tasks, decisions, constraints,
  failures and checkpoints with causal links and revision-checked updates without changing the
  canonical project graph;
- compact context and saved working maps reduce repeated source reading, which
  helps preserve token budget and keeps large, multi-step tasks coherent;
- incremental compile and watch mode keep the graph current without rebuilding
  unchanged files.

REQL is deterministic by default. Compilation, storage, query, retrieval,
analysis, reports, and MCP access work locally without mandatory LLM calls,
accounts, hosted services, or an external graph database. Optional semantic
adapters can exist at integration boundaries, but the core memory system remains
usable on its own.

## Quick Start

REQL requires Python 3.10 or newer. Install the package with pip:

```bash
python -m pip install reql
```

For local development from a checkout, install it in editable mode:

```bash
python -m pip install -e .
```

Install assistant instructions for the coding-agent environment:

```bash
reql install codex
```

Replace `codex` with another supported agent platform, or let interactive
install auto-detect one. The installed instructions make REQL part of the
agent's normal repository workflow. The generated `SKILL.md` is a concise
coding workflow: REQL retrieves source evidence and project direction, while the checked-out source and tests
remain authoritative. Bootstrap, query,
update, reporting, document, and Agent Workspace details stay in routed
`references/` files loaded only when their situation occurs.
Installation also adds the explicit-only `reql-context-compact` skill. Invoke
`/reql-context-compact` in agents that expose skills as slash commands (or
select that skill explicitly in Codex). It runs a local, model-free transcript
filter only when the host can export and accept a JSON message list. The filter
keeps every message and replaces only large, exact duplicate tool results with
pointers to an earlier retained copy. It never reads or changes hidden host
context on its own; Codex and other hosts without transcript replacement support
cannot compact the live window through this skill. For external harnesses, pipe
a transcript through `python -m agents.context_compact < transcript.json > compacted.json`,
then provide the output as the next message list. The source file is not modified.

For resumed or substantial work, start with `project overview` for compact goals,
architecture, workstreams, completed/active outcomes, blockers and next ready work.
Retrieve source context and relevant engineering evidence, then reconcile shared
records at meaningful work boundaries:

```bash
reql project status
reql project overview
reql query_context --query "serializer" --code
reql project context --file src/codec.py
reql agent init --name "Serializer contract"
reql agent record goal "Preserve serializer round trips" --key serializer
reql agent record task "Verify consumers" --parent GOAL_ID --file src/codec.py
reql agent record decision "Keep schema version" --parent TASK_ID --why "Consumer compatibility"
reql agent show TASK_ID --json
reql agent record task "Verify consumers" --id TASK_ID --revision 1 --status in_progress
reql agent task done TASK_ID "Consumer regression passed"
reql agent finish "Serializer updated; unresolved work retained"
```

Use the ids and revisions returned by your commands. Shared work survives session
completion; finish creates a checkpoint and releases private scratch. Dependencies
and conflicts derive readiness/blockers, and replacement flags obsolete assumptions.
`query_context` joins up to eight relevant work records to its source evidence;
`project context` supports task, file, workstream and history scopes. Identical
writes deduplicate; stale updates require reconciliation. See
[Engineering coordination](docs/COORDINATION.md) for lifecycle, retrieval,
retention, migration and Python ownership details. `project explain` drills into
architecture; `project overview --details` includes full architecture and legacy
history; `agent show ID --json` exposes record evidence and bounded revisions.

Code-scoped `query_context` retains its eight-path source budget, owner symbols,
bounded line ranges and associated tests. JSON remains schema-v2, adding
`payload.engineering_context` with its own revision alongside source-only graph
revision/confidence. Low code confidence still allows one targeted exact-name
search; engineering claims must be checked against current source and tests.

Context results use schema version 2. Alongside the query-specific
`graph_revision`, they report the committed `source_revision` and freshness
state (`current`, `refreshing`, `stale`, or `unknown`).

From a source checkout, `python cli.py ...` exposes the same command surface
without requiring an editable install:

```bash
python cli.py project compile
python cli.py query_context --query "payment service"
```

Use the Python API directly:

```python
from reql import MemoryGraph

graph = MemoryGraph.open(".reql/memory.reql")

try:
    graph.compile_project(".")

    context = graph.query_context("payment service")
    print(context)
finally:
    graph.close()
```

Start MCP when an integration needs a tool server:

```bash
reql-mcp --read-only
```

Project/cache commands and `reql storage clear` default storage to
`./.reql/memory.reql`; other commands default to
`./.reql/memory.reql`. `storage clear` rebuilds that store from the current
project tree and discards historical or archived graph state. Use `--json` for
automation. See [docs/CLI.md](docs/CLI.md)
for the complete command reference, query modes, install behavior, MCP startup,
config lookup, reports, exports, and maintenance workflows.

Use `reql config set OPTION VALUE` to add or update a validated setting in the
local `./reql.conf`, for example `reql config set retention.agent_sessions 30`.

Automatic project maintenance uses `retention.commits` from `reql.conf`
(default `20`). A REQL commit is a successful compilation that changes the
project manifest and creates a `ProjectRevision`; clean compile invocations do
not advance retention. When the limit is exceeded, REQL removes history,
archived records, and project-owned usage entries older than the oldest retained
commit while preserving the active graph. Agent coordination records retain
public summaries for the latest `retention.agent_sessions` completed sessions
across the project (default `20`). Set it to `0` to discard completed public
history immediately. Completed agents' private scratch stores are removed regardless
of that limit. Shared engineering records have independent bounded retention and
are preserved even when `retention.agent_sessions` is zero.

## Features

- Local project compilation into a property graph with explicit provenance.
- Retrieval with lexical seed nodes, bounded graph expansion, and chain-aware ranking.
- Compact `query_context`, `query_explore`, `query_graph`, and `query_memories`
  outputs for coding-agent workflows, including owner symbols, bounded source
  ranges, associated test targets and scoped engineering evidence.
- Shared structured engineering work with revisions, causal relations, derived
  execution and checkpoints; separate private scratch and session lifecycle.
  It contains no copied project, file, symbol, or canonical graph records.
- Local block-file persistence with fixed-size pages, compressed records,
  reader/writer lock diagnostics, safe stale-lock recovery, transactions,
  compaction, and atomic clean rebuilds.
- Incremental compilation cache with persistent compilation runs and graph deltas.
- Artifact document parsing for Markdown, plain text, and PDF with graceful
  fallbacks.
- Code artifact recognition for Python, TS/JS, Go, Rust, Java, C/C++,
  Ruby, C#, Kotlin, Scala, PHP, Swift, Lua, Zig, PowerShell, Elixir, Julia,
  Verilog, Fortran, Bash, SQL, Terraform, Apex, Pascal, Razor, and related
  extensions, with Tree-sitter AST graph extraction for recognized languages.
- Static-analysis findings for cleanup-oriented queries, including aggregated
  orphan-directory candidates so a detached folder is suggested once instead
  of file by file.
- Deterministic community detection, hub analysis, and bridge analysis with
  generic-node penalties.
- Markdown reports, JSON export, standalone `graph.html`, interactive project
  pipeline HTML and Mermaid export, guided launcher, installable CLI, typed
  Python API, and optional dependency-free MCP server.

## Runtime Model

REQL works as a local repository index backed by a property graph:

- `project compile` scans the project with default ignores plus configured
  include/exclude rules;
- each artifact is fingerprinted, so unchanged files can be skipped on later
  compiles;
- supported code files are parsed into modules, symbols, imports, calls,
  dependencies, endpoints, config records, tests, and static-analysis findings;
- supported documents are split into source fragments, ranked document terms,
  raw observations, and document-to-code links when they explicitly name code
  symbols;
- graph records keep file paths, line ranges, evidence, confidence, and
  provenance so query results can be traced back to source;
- queries find lexical seed nodes, expand only a bounded graph neighborhood,
  rank the resulting records, and render compact context instead of dumping the
  repository;
- `project explain` projects technical code facts into business capabilities,
  architectural layers, multi-evidence semantic workflows with explicit
  `implemented_by` participants, and focus-specific change guidance without
  persisting another graph or requiring an LLM;
- `project pipeline` follows every detected entrypoint through project-local
  flow relations, collapses symbols into shared architectural components, and
  writes an interactive `pipeline.html` or Mermaid `pipeline.mmd`;
- `project compile` and watch mode reuse the same incremental compiler and
  write `CompilationRun`, `GraphDelta`, and cache records for changed or
  deleted artifacts.

The core path is deterministic and local. Optional semantic adapters can exist
at integration boundaries, but project compilation, storage, retrieval, reports,
analysis, and MCP tools do not require model calls.

## Interactive Launcher

The project root `launcher.py` starts a guided terminal menu when run without
arguments. It uses `./.reql/memory.reql` in the current working directory by default and lets you choose actions
interactively without writing command-line arguments:

```bash
python launcher.py
```

You can pass a storage path directly:

```bash
python launcher.py --storage .reql/memory.reql
```

The menu guides these workflows:

- create or open a graph storage file and initialize it;
- list available graph files under `.reql/`, open them, inspect them, or
  delete managed `.reql` files after an explicit name confirmation;
- retrieve ranked code records or compose bounded agent context;
- scan and incrementally compile projects;
- run predefined REQL queries or custom graph queries;
- inspect stats, communities, hubs, and graph analysis;
- export Markdown reports, JSON, and standalone `graph.html`;
- print Codex and Claude Desktop MCP configuration for `reql-mcp`.

For command-line automation, use `python cli.py ...` or `reql`:

```bash
reql project compile
reql query_memories --query "payment service" --limit 5 --json
reql query "FIND nodes WHERE type = 'Function' LIMIT 10"
```

## Documentation

The README gives the project overview and common workflows. The focused
documentation lives under `docs/`:

- [Architecture](docs/ARCHITECTURE.md): layers, storage port, compile flow,
  retrieval, maintenance, and deterministic analysis.
- [Repository explanation](docs/REPOSITORY_EXPLANATION.md): deterministic
  business capabilities, architecture, workflows, and change guidance derived
  from the code graph.
- [Engineering coordination](docs/COORDINATION.md): shared work, plans, checkpoints,
  context ranking, migration and lifecycle.
- [CLI](docs/CLI.md): command reference for compilation, retrieval, graph
  queries, exports, configuration, install helpers, and MCP startup.
- [Configuration](docs/CONFIGURATION.md): `reql.conf`, defaults, overrides, scan rules,
  cache settings, document ingest, analysis toggles, and loader behavior.
- [REQL language](docs/REQL.md): first-class graph commands plus `FIND`,
  `MATCH`, `PATH`, `SEARCH`, `RETRIEVE`, `EXPLAIN`, filters, and examples.
- [Schema](docs/SCHEMA.md): core node records, edge records, code types, and
  relations used in the graph.
- [Storage](docs/STORAGE.md): block adapter, data locality, compression,
  inspection, compaction, locking, transactions, and bounded operations.
- [Artifact ingestion](docs/ARTIFACT_INGESTION.md): supported document inputs,
  parser behavior, graph output, and graceful fallbacks.
- [Code analysis](docs/CODE_ANALYSIS.md): language support, Tree-sitter parsing,
  extracted symbols, static-analysis findings, and example queries.
- [Incremental compilation](docs/INCREMENTAL_COMPILATION.md): dirty planning,
  cache entries, deltas, deletion handling, and failure behavior.
- [Graph analysis](docs/GRAPH_ANALYSIS.md): deterministic communities, hubs,
  bridge signals, CLI usage, REQL examples, and known analysis limits.
- [Reporting](docs/REPORTING.md): generated Markdown reports and standalone
  HTML graph export.
- [MCP server](docs/MCP.md): stdio/HTTP server startup, tool descriptions,
  Codex and Claude configuration, workflow, and security notes.
- [Extending](docs/EXTENDING.md): storage adapters, extractors, engines, and
  adding node or edge types.

## Architecture

For component ownership, source layout, compilation, retrieval, and maintenance,
see [Architecture](docs/ARCHITECTURE.md). The detailed processing contracts and
recovery behavior are documented in [Incremental compilation](docs/INCREMENTAL_COMPILATION.md),
[Artifact ingestion](docs/ARTIFACT_INGESTION.md), and [Engineering coordination](docs/COORDINATION.md).

## Public API

The main entry point is `MemoryGraph`. The canonical public import is
`from reql import MemoryGraph`.

```python
from reql import MemoryGraph

graph = MemoryGraph.open(".reql/memory.reql")
```

Main operations:

- `retrieve(query)`
- `compose_context(query)`
- `query_context(query)`
- `query_context_result(QueryContextRequest(...))`
- `query_context_payload(query)`
- `query_explore(query)`
- `query_graph(query)`
- `query_memories(query)`
- `query_memories_payload(query)`
- `locate(path)`
- `inspect_node(node_id)`
- `export_json()`
- `query(statement)`
- `compile_project(path)`
- `update_project(path)`
- `watch_project(path)`
- `project_status(path)`
- `project_history(path)`
- `project_revision(revision_id)`
- `project_report(path, output_dir=...)`
- `project_pipeline(path)`
- `cache_status(path)`
- `clear_cache(path)`
- `list_deltas()`
- `show_delta(delta_id)`
- `detect_communities(project_id=...)`
- `analyze_hubs(project_id=..., limit=...)`

The typed query-context API is available from the canonical package:

```python
from reql import MemoryGraph, QueryContextRequest, QueryMode, RetrievalBudget

request = QueryContextRequest(
    text="payment service",
    mode=QueryMode.INFORMATIVE,
    scopes=frozenset({"code"}),
    budget=RetrievalBudget(top_k=20, max_depth=3, max_items=20),
)
result = graph.query_context_result(request)
payload = result.to_dict()
```

## License

MIT. See `LICENSE`.

## Contributing

See `CONTRIBUTING.md` for development setup, contribution guidelines, and pull
request expectations.

For repeatable agent-session performance measurements and profiling, see
[Benchmark instructions](CONTRIBUTING.md#benchmarks). Fixtures and profiles remain local
under an ignored directory; benchmarks do not use provider APIs or model downloads.


