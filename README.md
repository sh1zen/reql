# REQL

REQL is a local repository context and working-memory layer for coding agents
and developer tools. It compiles source files and supported documents into a
property graph, then answers bounded queries over code, symbols, tests,
documents, dependencies, findings, and provenance.

In the intended coding-agent integration, the user does not treat REQL as a
separate manual workflow. After the assistant instructions or skill are
installed for Codex, Claude, Gemini, Cursor, or another agent environment, the
agent uses REQL while it works: it compiles or refreshes the repository graph,
retrieves compact source-backed context, records task-local notes and
decisions, links work back to files and symbols, and reconstructs that working
set after context loss.

**Token and reasoning budget:** REQL helps coding agents spend fewer tokens on
repository discovery and more tokens on the actual change. Bounded retrieval
returns the files, symbols, relationships, and source spans that matter for the
current task, while Agent Workspace preserves the task map, decisions, risks,
and handoffs needed to reason through complex or large implementations across
context windows.

The important part is that REQL gives the agent deterministic repository memory
before and during edits:

- `project compile` scans the project, fingerprints artifacts, parses supported
  code and documents, and writes graph nodes, edges, cache records, compilation
  runs, and deltas;
- retrieval commands such as `query_context`, `query_explore`, `query_graph`,
  and `query_memories` find lexical seed nodes, expand a bounded graph
  neighborhood, rank the result, and return compact source-backed context;
- every result can point back to paths, line ranges, relationships, evidence,
  and graph provenance instead of relying on broad source dumps;
- `reql agent` maintains per-agent working memory for plans, findings,
  decisions, tasks, risks, file links, and handoffs without changing the
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
agent's normal repository workflow. The commands below are the operations the
agent integration uses to bootstrap context, retrieve focused evidence, and
keep working memory:

```bash
reql project compile .
reql query_context --query "payment service"
reql query_memories --query "payment service" --limit 8 --json
reql query_explore --query "payment service serialization" --view owners --view code
```

Agent Workspace commands store the agent's session-scoped working state while
it implements, reviews, or documents a repository:

```bash
reql agent init
reql agent bus
reql agent session start "Focused implementation pass"
reql agent add "Read src/memory/cli.py and found the argparse command surface"
reql agent task add "Implement reset for the working graph"
reql agent decision add "Keep agent memory in .reql/agent.reql"
reql agent link TASK_ID artifact:app --relation touches
reql agent link-task --file test-agent/context_savings.py
reql agent link-many TASK_ID artifact:app function:target --relation implements
reql agent batch --json agent-ops.json
reql agent batch --task task="Patch CLI" --decision decision="Use one workspace lock" --link '$task' implements '$decision'
reql agent map --session current
reql agent handoff "Implementation notes ready for master review"
reql agent export --json
reql agent export --json --metadata
```

`reql agent init` returns an `agent_id` and makes that private agent memory the
current one for later `reql agent ...` commands. Parallel agents can use
`reql agent --agent AGENT_ID ...` or `REQL_AGENT_ID=AGENT_ID`; all agents can
read `reql agent bus`, publish shared messages, and use `reql agent handoff` to
return a compact saved working-map snapshot to the master. `agent bus --json`
omits handoff payload snapshots by default; pass `--include-payloads` only when
the full saved handoff maps are needed. `agent map`, `agent search`, and
`agent export` omit metadata by default; use `agent map --session current
--completed` for a completed session summary after tasks are marked done. Pass
`--metadata` only when timestamps, storage paths, source fields, or the full
workspace graph are needed.

`reql agent reset` discards agent-created working notes and re-derives the
workspace from the current standard graph without modifying `.reql/memory.reql`.

From a source checkout, `python cli.py ...` exposes the same command surface
without requiring an editable install:

```bash
python cli.py project compile .
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

Project/cache commands default storage to `<project>/.reql/memory.reql`; other
commands default to `./.reql/memory.reql`. Use `--storage`, `--config`, `--set`,
and `--json` for automation. See [docs/CLI.md](docs/CLI.md) for the complete
command reference, query modes, install behavior, MCP startup, config lookup,
reports, exports, and maintenance workflows.

## Features

- Local project compilation into a property graph with explicit provenance.
- Retrieval with lexical seed nodes, bounded graph expansion, and chain-aware ranking.
- Compact `query_context`, `query_explore`, `query_graph`, and `query_memories`
  outputs for coding-agent workflows.
- Separate `reql agent` working graph for agent notes, tasks, decisions,
  findings, plans, risks, and links without contaminating the standard graph.
- Local block-file persistence with fixed-size pages, compressed records,
  locking, transactions, and compaction.
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
- Markdown reports, JSON export, standalone `graph.html`, guided launcher,
  installable CLI, typed Python API, and optional dependency-free MCP server.

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
- `project update` and watch mode reuse the same incremental compiler and write
  `CompilationRun`, `GraphDelta`, and cache records for changed or deleted
  artifacts.

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
reql project compile .
reql query_memories --query "payment service" --limit 5 --json
reql query "FIND nodes WHERE type = 'Function' LIMIT 10"
```

## Documentation

The README gives the project overview and common workflows. The focused
documentation lives under `docs/`:

- [Architecture](docs/ARCHITECTURE.md): layers, storage port, compile flow,
  retrieval, maintenance, and deterministic analysis.
- [CLI](docs/CLI.md): command reference for compilation, retrieval, graph
  queries, exports, configuration, install helpers, and MCP startup.
- [Configuration](docs/CONFIGURATION.md): `conf.yaml`, overrides, scan rules,
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

## Main Pipeline

REQL has a project compile pipeline. `compile` builds a technical graph for
programming agents from scanning, AST/static analysis, document parsing, and
deterministic document-to-code linking. It does not extract memories from chat
or non-code prose.

Retrieval:

```text
query
  -> tokenization
  -> lexical seed-node search
  -> bounded graph expansion
  -> graph-aware ranking
  -> subgraph or context block
```

Maintenance:

```text
activation and usage signals
  -> salience update
  -> sidecar retrieval usage updates
  -> provenance preservation
```

Compile-time project scan:

```text
project directory
  -> recursive scanner
  -> default ignore rules and config include/exclude filtering
  -> file classification and SHA-256 fingerprints
  -> Project + Directory + File + SourceArtifact nodes
  -> CONTAINS edges
```

Project compile uses the same fingerprinting path and built-in default ignores.
Put additional compile exclusions in the configured `scan.exclude` list.

Incremental compilation:

```text
SourceArtifact fingerprints
  -> ArtifactCacheEntry comparison
  -> dirty and deleted artifact set
  -> deterministic compile graph updates
  -> CompilationRun
  -> GraphDelta
```

Artifact document parsing:

```text
SourceArtifact bytes
  -> document parser
  -> DocumentFragment records
  -> SourceFragment nodes
  -> provenance and document relations
```

Code analysis:

```text
code artifact
  -> AST/static parser when supported
  -> Module / Package / Class / Interface / Function / Method / useful Variable nodes
  -> Import / Dependency / Endpoint / Schema / Config / Test nodes
  -> CONTAINS / DEFINES / METHOD / IMPORTS / CALLS / REFERENCES / INHERITS edges
  -> DEPENDS_ON / IMPORTS_FROM / RE_EXPORTS / READS / WRITES / RETURNS edges
  -> RAISES / DECORATED_BY / HANDLES_ROUTE / HAS_FINDING edges
  -> StaticAnalysisFinding cleanup candidates for unused code
```

Graph analysis:

```text
graph nodes and edges
  -> deterministic community detection
  -> specificity-aware hub scoring
  -> cross-community bridge edges
  -> Community nodes, BRIDGES_COMMUNITY edges, and hub properties
```

## Project Structure

```text
src/api/
|-- memory_graph.py         # Public facade
|-- __init__.py             # Public Python API exports
src/agents/
|-- install.py              # Agent skill/instruction installers
|-- __init__.py             # Agent installer exports
src/mcp/
|-- tools.py                # Dependency-free MCP tool handlers
`-- server.py               # stdio and HTTP JSON-RPC MCP transports
src/memory/
|-- domain/                 # Pure models, constants, ids, time, exceptions
|-- ports/                  # Storage and extractor protocols
|-- infrastructure/         # Concrete adapters
|   `-- block/              # BlockGraphStore
|-- extraction/             # Deterministic query/source extraction and optional adapters
|-- artifacts/              # Project scanning, file classification, fingerprints
|-- engines/                # Numeric and maintenance engines
|   |-- activation.py       # Spreading activation
|   `-- salience.py         # Salience scoring
|-- services/               # Application orchestration
|   |-- retrieval.py
|   |-- incremental_compilation.py
|   `-- project_watch.py
|-- query/                  # REQL lexer, parser, AST, and evaluator
|-- analysis/               # Communities, centrality, specificity, hubs, bridges
|-- reporting/              # Markdown reports
|-- config/                 # conf.yaml models and loader
`-- cli.py                  # Command-line interface
```

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
- `query_explore(query)`
- `query_graph(query)`
- `query_memories(query)`
- `query_memories_payload(query)`
- `export_json()`
- `query(statement)`
- `compile_project(path)`
- `update_project(path)`
- `project_status(path)`
- `project_report(path, output_dir=...)`
- `cache_status(path)`
- `clear_cache(path)`
- `list_deltas()`
- `show_delta(delta_id)`
- `detect_communities(project_id=...)`
- `analyze_hubs(project_id=..., limit=...)`

## Extending the Project

### New Storage Backend

Implement `memory.ports.graph_store.GraphStore` and pass it to the facade:

```python
from reql import MemoryGraph

store = MyGraphStore(...)
graph = MemoryGraph(store)
```

The bundled block backend is portable local persistence, not an architectural
constraint.

### New Extractor

Implement `SemanticExtractor`:

```python
class MyExtractor:
    def extract(self, text: str):
        ...

graph = MemoryGraph.open(".reql/memory.reql", extractor=MyExtractor())
```

The extractor is used for query seed discovery. Project document ingest is
handled by the local deterministic compiler path. The default
`MemoryGraph.open()` extractor is dependency-free and deterministic.

Compile mode structurally parses text document fragments and links explicit
documentation mentions back to compiled code symbols where possible.

### New Node or Edge Types

Types are strings. To keep them coherent:

1. add constants in `domain/constants.py`;
2. update compiler, retrieval, reporting, or analysis code if dedicated logic is needed;
3. add integration tests when the new type changes retrieval, salience, or graph analysis.

## License

MIT. See `LICENSE`.

## Contributing

See `CONTRIBUTING.md` for development setup, contribution guidelines, and pull
request expectations.


