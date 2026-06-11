# REQL

REQL is a graph-native, storage-agnostic memory engine for codebases and
developer tools. It scans a project, compiles source files and supported
documents into a local property graph, and retrieves focused context without
mandatory LLM calls.

The purpose of REQL is to give coding agents useful, bounded repository context
for making code changes. Instead of forcing an agent to scan the entire project
or rely on whatever files happen to fit in the prompt, REQL helps it locate the
code, symbols, documents, tests, relations, and provenance that matter for the
current task.

This keeps modification work grounded in the project graph: agents can start
from compact context, follow important links between files and symbols, avoid
missing relevant connections, and reduce token waste from broad source dumps.
LLMs can be attached at the edges as optional adapters, but compilation,
storage, query, retrieval, analysis, reports, and MCP access work locally.

The graph layer is code-first: projects, directories, files, source fragments,
modules, packages, classes, interfaces, functions, methods, imports,
dependencies, static-analysis findings, document fragments, communities, hubs,
and bridges are stored as explicit nodes and edges with provenance.

At a high level REQL works in five stages:

1. scan a project with configured include/exclude rules and built-in ignores;
2. fingerprint artifacts so unchanged files can be skipped on later runs;
3. parse supported code and document artifacts into deterministic graph records;
4. persist graph nodes, edges, cache entries, compilation runs, and deltas in a
   local store;
5. answer queries by finding lexical seed nodes, expanding a bounded graph
   neighborhood, ranking the result, and rendering compact context.

This makes REQL useful as a repository context index and deterministic
retrieval layer for coding agents: it narrows the working set before edits,
keeps important relationships visible, and stays independent from any one
database, model provider, or editor.

## Features

- Property graph with nodes, edges, weights, confidence, polarity, and provenance.
- Local block-file persistence with fixed-size pages and compressed records.
- Retrieval with lexical seed nodes, bounded graph expansion, and chain-aware ranking.
- Compact `query_memories` retrieval with agent-ready memory/source texts plus ranked-node, edge, source, and trace metadata.
- Support for reinforcing and inhibiting edges.
- Salience scoring to rank useful code entities and findings.
- Decay and archival of weak graph records while preserving the audit trail.
- Markdown code graph reports.
- Guided terminal launcher and installable CLI.
- REQL, a small language for querying the graph.
- Compile-time project scanning that registers files as graph artifacts.
- Incremental compilation cache with persistent compilation runs and graph deltas.
- Artifact document parsing for Markdown, plain text, and PDF with graceful
  fallbacks.
- Code artifact recognition for Python, TS/JS, Go, Rust, Java, C/C++,
  Ruby, C#, Kotlin, Scala, PHP, Swift, Lua, Zig, PowerShell, Elixir, Julia,
  Verilog, Fortran, Bash, SQL, Terraform, Apex, Pascal, Razor, and related
  extensions, with Tree-sitter AST graph extraction for recognized languages.
- Deterministic community detection, hub analysis, and bridge analysis with
  generic-node penalties.
- Optional dependency-free MCP server for bounded agent context retrieval.
- `conf.yaml` configuration for scanning, parsing, cache behavior, analysis
  toggles, and report output.
- Typed Python API.

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

## Requirements

- Python 3.10 or newer.
- `watchdog` is optional and required only for filesystem monitor mode.

## Installation

To work on the project locally:

```bash
python -m pip install -e .
```

To run the tests without an editable install:

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

On PowerShell:

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
```

## Python Quick Start

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

## CLI Usage

The canonical installed command is `reql`. If `--storage` is not provided,
project and cache commands save data in `<build path>/.reql/memory.reql`; other
commands use `./.reql/memory.reql` from the current working directory.

From a source checkout, `python cli.py ...` is the repository-local CLI entry
point. It works without `PYTHONPATH` changes or an editable install:

```bash
python cli.py project compile path/to/project
python cli.py query_context --query "payment service"
python cli.py query_memories --query "payment service" --json
```

Use `python launcher.py` for the guided terminal menu:

```bash
python launcher.py
```

Running `python launcher.py` without arguments opens a guided menu for project
compilation, retrieval, REQL queries, reports, MCP setup, and storage
management. Use `python cli.py ...` or the
installed `reql` command for command-line automation.

Compile a repository:

```bash
reql project compile path/to/project
```

Document fragments are parsed structurally when `compile.ingest_documents` is
enabled and can be linked to code symbols, while remaining lower-level source
context for query answers. `compile.documents` controls which document formats
are structurally ingested. Ingested documents also pass through a local
deterministic processor that ranks language-agnostic terms, records `RawEvent`
observations, creates co-occurrence relations, and connects document terms to
code symbols when the document explicitly names them.

Retrieve compact agent-ready memories:

```bash
reql query_memories --query "payment service" --limit 8 --json
```

`query_memories` is the unified compact retrieval path for agents. It returns
deduplicated memory/source text rows and, in JSON mode, includes useful retrieval
metadata such as `trace_id`, `seed_node_ids`, `ranked_nodes`, `nodes`, `edges`,
`edge_directions`, `sources`, and `counts`. Use `reql query "RETRIEVE ... RETURN
..."` when you need explicit custom REQL columns, and use `query_context` when
an agent needs a synthesized context block with owner targets, snippets, source
evidence, or cleanup guidance.

Compose context for an agent:

```bash
reql query_context \
  --query "payment service"

reql query_explore \
  --query "payment service serialization" \
  --view owners \
  --view callers \
  --view serialization_paths \
  --json

reql query_graph \
  --query "payment service" \
  --max-depth 2

reql query_memories \
  --query "payment service" \
  --limit 8
```

`query_context` returns a compact deterministic context block for an agent. It
is informative by default for structure, documents, existence checks, and graph
links, with source file/line references and raw-query references in one block.
Add `--code`, `--docs`, or `--test` to limit the same query to code,
documentation/imported documents, or tests. Add `--cleanup` for dead-code and
unused-symbol cleanup; cleanup output shows only matching `StaticAnalysisFinding`
candidates.
Add `--json` to get compact structured data without duplicated rendered
Markdown, including `query_mode`, `scopes`, `owner_candidates`,
`cleanup_candidates`, `working_set`, and `targeted_reads`.
`query_explore` returns targeted dependency slices for coding agents: owners,
callers, public API surface, serialization paths, docs mentions, and code
working-set records. Its `code` view includes the same usage guidance, snippets,
and targeted reads. Use repeated `--view` flags or shortcuts such as
`--owners-only`, `--callers-only`, and `--serialization-paths-only` to reduce
token use when a task only needs one chain.
`query_graph` returns an agent-oriented structured subgraph: query seed nodes,
expanded nodes and edges up to the requested depth, linked textual sources,
generic-node filtering diagnostics, directed incoming/outgoing edge context,
and a compact rendered context block. Add `--json` when another tool or agent
should consume the full payload. By default, code contexts prioritize files,
modules, symbols, resolved calls, imports, source snippets, and unused-code
cleanup findings while hiding unresolved callsites, comments, docstrings, and
generic topics unless they are directly queried.
`query_memories` returns the ranked memory/source text list and, with `--json`,
the retrieval details agents otherwise had to collect separately from graph
lookups.
Query/retrieval commands record usage in an append-only overlay rather than
rewriting canonical graph records. Ranking can use that overlay immediately.
Read/query commands open the graph read-only directly, so parallel readers can
run together. Writers acquire an exclusive lock, wait for active readers to
finish, and block new readers until the write session opens safely.
Refresh project maintenance data:

```bash
reql project compile .
reql cache status .
reql query "DELTAS LIMIT 10"
```

Code graph maintenance is handled by incremental compile, cache/delta tracking,
salience, and deterministic graph analysis.

Generate reports, statistics, and exports:

```bash
reql stats
reql query "EXPLAIN HUB 'NODE_ID'" --json
reql storage inspect
reql storage compact
reql export --out graph.json
reql export --json --out reql-json
reql export --html --out graph.html
reql export --html --json --out reql-graph-out
```

Many commands support `--json` for machine-readable output.

When compile or query latency is unclear, set
`diagnostics.enabled = true` and `diagnostics.path = ".reql/profile.jsonl"` in
`conf.yaml`. REQL appends JSONL timing events for storage open, read-only
opens, compile phases, per-artifact compilation, REQL query
parse/evaluation, retrieval phases, and `graph.close`.

Install assistant instructions for REQL:

```bash
reql install codex --project
reql install claude --project
reql install --project
reql install codex,claude --dry-run
reql install claude --project --no-hooks
reql install codex --project --command-dir ~/.local/bin
reql uninstall codex,claude --project
```

`reql install` auto-detects supported coding-agent profiles by default and
writes idempotent skill/instruction files for Claude Code,
Codex, OpenCode, Kilo Code, Cursor, Gemini CLI, GitHub Copilot CLI,
VS Code Copilot Chat, OpenClaw, Hermes, Kimi Code, Google Antigravity, and
generic AGENTS-compatible clients. Project scope writes files under the current
project, while the default profile scope writes to the assistant profile under
your home directory. Profile scope also installs always-on guidance where each
agent expects it, including `~/AGENTS.md` for Codex, `~/.claude/CLAUDE.md` for
Claude Code, `~/GEMINI.md` for Gemini CLI, and agent-profile `AGENTS.md`,
rules, or instruction files for the other supported clients. Pass explicit
platform names, or `--all`, when you want to override auto-detection. It also installs a REQL-owned
`reql` command shim that points back to this checkout or installed package; use
`--command-dir` to choose the shim directory explicitly. The installed guidance
tells agents to prefer `reql`, but also includes the absolute shim path and a
Python fallback so the workflow does not depend on `PATH` alone. Agents use
deterministic REQL commands such as `reql project status .`,
`reql project compile .`, `reql project update .`, `reql project compile . --watch`, `reql query_context --query "..."`,
`reql query_memories --query "..."`, and `reql query_graph --query "..."`
as the repository context index before source exploration. Generated guidance
instructs agents not to duplicate that context with broad `rg`, recursive
directory listings, custom scanners, or other repository-wide discovery
commands; targeted file reads remain for exact edits, debugging, and tests.
Generated profile guidance treats `/reql` as the explicit trigger for the REQL
workflow, does not treat dirty `.reql/` files as a reason to skip REQL, and
uses `reports/GRAPH_REPORT.md` only for broad review when bounded queries are
not enough.
Skill-capable clients receive the generated project context skill
(`reql-project`). The skill includes `SKILL.md`, Codex-facing
`agents/openai.yaml` metadata, and focused `references/` for bootstrap/query,
updates/watch mode, reports/exports/MCP, and optional document semantics.
Agents check `reql project status .`; if the project has not been built yet,
they must immediately run `reql project compile .` before broad raw file
exploration.
For unused-code cleanup, generated skills direct agents to start from REQL
`FINDINGS` and `StaticAnalysisFinding` queries, then validate candidates with
targeted source reads or symbol searches before recommending removals. The
guidance separates direct cleanup such as unused imports or variables from
public/API-risk candidates such as functions, methods, classes, entry points,
callbacks, and dynamically referenced hooks.
During active editing, `--watch` is the monitor mode: from the agent's working
directory, keep one watcher running so REQL performs an initial cache check and
then updates memory from detected file changes instead of rebuilding on every
interaction. If no watcher is running, generated guidance tells agents to run
`reql project compile .` once after modifying project files before finishing, so
the graph reflects the completed edits. The generated skill also tells agents to
confirm this graph update path before the final response for any task that
changed files. Installs write `.reql_agent_version` stamps
for clean upgrades;
`reql uninstall` removes REQL-owned files, managed sections, version stamps, the
owned command shim, and automatic hooks. It does not overwrite or delete a
pre-existing `reql` command that was not generated by this installer. Claude
and Gemini hooks are installed by default and can be skipped with `--no-hooks`.
Accepted platform names are canonical names such as `claude`, `codex`,
`opencode`, `kilo`, `cursor`, `gemini`, `copilot`, `openclaw`, `hermes`,
`kimi`, and `antigravity`.

The optional MCP server exposes bounded REQL tools for Codex, Claude Desktop,
and other MCP clients:

```bash
reql-mcp
reql-mcp --config conf.yaml --set project.id=agent-a --read-only
reql-mcp --transport http --host 127.0.0.1 --port 8765 --api-key "change-this-key"
reql-mcp --transport http --host 0.0.0.0 --port 8765 --api-key "change-this-key" --read-only
```

The default MCP transport is stdio. The HTTP transport serves JSON-RPC at
`/mcp` and requires `Authorization: Bearer <api-key>` for sharing across
processes or trusted machines. See [docs/MCP.md](docs/MCP.md) for tool
descriptions, HTTP details, and client configuration.

`export --html` creates a standalone browser view of the stored memory graph.
Use it to inspect hubs, clusters, neighbors, source paths, and relation labels
without starting a server. The HTML view is meant for exploration and sharing;
large stores render a bounded connected visual core so the graph stays dense and
responsive, using a static layout without automatic movement. Use `--json` when you need the complete graph data for backup,
automation, or another tool. When `--out` is a directory-like path, `graph.html` is written
inside it; `--json` also writes the matching complete `graph.json` next to the
HTML file.
Use `export --json` without `--html` to write `graph.json` to disk instead of
printing the JSON payload to stdout.

Configuration can be initialized and inspected from the CLI:

```bash
reql config init
reql config show
reql --set scan.max_file_size_mb=2 --set cache.enabled=false project compile .
reql project compile . --watch
```

The repository root `conf.yaml` is the canonical default configuration and
contains every supported project config option. It configures project id defaults, scan limits,
include/exclude globs, parser enablement, cache behavior, compile document
semantics, analysis toggles, and report output. Project and cache commands
search for `conf.yaml` from the target project path upward, so each project can
carry its own config. If none exists, REQL falls back to the canonical config at
the REQL code root. `REQL_CONFIG` can select a config file for wrappers and
agents, and `REQL_CONFIG_OVERRIDES`, CLI `--set`, or MCP `config_overrides` can
override config values without editing the default file. Command-specific flags
override matching config values.

Compile or inspect a repository or project directory:

```bash
reql project compile .
reql project update .
reql project compile . --watch
reql project exclude ".tmp/" "generated/*.json"
reql project status .
reql project report . --output reports/
reql cache status .
reql query "DELTAS LIMIT 10"
reql query "COMMUNITIES LIMIT 20"
reql query "HUBS LIMIT 20"
```

`project compile` scans read-only first, skips unchanged artifacts using the
project-local `.reql/artifact-cache.json` fingerprints, writes only
changed/deleted artifact deltas, and leaves the graph complete and query-ready
when the command returns. `ArtifactCacheEntry` graph nodes mirror the disk
cache for query/report inspection and recovery.
If cache metadata is missing but the directory was already compiled, REQL
recovers cache entries from matching compiled `SourceArtifact` records instead
of recompiling every file from zero.
Compile mode applies built-in default ignore rules for dependency, VCS, cache,
build-output, and local database paths, then applies configured include/exclude
patterns and file-size limits.
Use `project exclude PATTERN [PATTERN ...]` only for explicit exclusions or
obvious dependency/cache/build-output directories. It creates or updates the
runtime project's `conf.yaml`, appends rules to `scan.exclude`, defaults to the
current working directory, and is idempotent. Pass all patterns in one command,
do not use workspace-wide patterns such as `*`, `**`, or `**/*`, and do not
exclude source/framework roots needed for the task.
Use `project update` for a manual incremental refresh of the same project path.
Use `cache status` to see clean, dirty, and deleted counts; `cache clear`
archives project-local cache metadata without removing graph data. Use
`reql query "DELTAS ..."` to inspect persisted deltas.

Use `project compile . --watch` from the working directory while Codex, Claude, or
another agent is editing the project. It uses Python `watchdog` filesystem
events and automatically recompiles only dirty or deleted artifacts. This is
monitor mode: leave it running during active work instead of launching repeated
manual compile/rebuild loops. The watch process runs until interrupted; use
`--watch-iterations` for bounded scripts or pass an explicit path when the
workspace is elsewhere.

Document parsers extract Markdown structure, plain-text paragraphs, and PDF page
text when an optional parser is installed. Missing PDF dependencies degrade to
metadata and parser-error records instead of becoming mandatory runtime
dependencies. Image and video files are skipped and are not registered as
artifacts. Compile keeps text documents structural.

Code artifacts are recognized across common programming languages including
Python, TS/JS, Go, Rust, Java, C/C++, Ruby, C#, Kotlin, Scala, PHP, Swift, Lua,
Zig, PowerShell, Elixir, Julia, Verilog, Fortran, Bash, SQL, Terraform, Apex,
Pascal, and Razor. Recognized languages are parsed through Tree-sitter AST
grammars declared by the package. Python, JavaScript, and TypeScript keep the
richest extraction path for imports, classes, functions, methods, meaningful
variables, resolved calls, cleanup findings, comments, and docstrings; the
generic Tree-sitter path for other languages emits modules, major declarations,
imports/includes where exposed by the grammar, comments, and source fragments
without falling back to regex or standard-library AST parsers. Unresolved
low-signal calls are summarized on the owning symbol instead of becoming
standalone call-site nodes, and builtin or stdlib-like synthetic targets are
filtered.

Graph analysis can detect communities, rank useful hubs, and identify specific
evidence-backed bridges between communities. Hub scoring combines degree,
weighted degree, usage signals when available, salience, community bridging,
specificity, and confidence while penalizing generic high-degree nodes.

Project reports can be written after scan or compile:

```bash
reql project report . --output reports/
```

This creates `GRAPH_REPORT.md`, `GRAPH_DELTAS.md`, and `CACHE_REPORT.md` with
project structure, cache state, deltas, ingestion status, code symbols,
communities, hubs, bridge signals, and memory health.

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

## REQL

The CLI includes the `query` command for querying the graph with a textual
statement passed as an argument:

```bash
reql query "FIND nodes WHERE type = 'Claim' LIMIT 10"
```

Results can be printed as a table or as structured JSON.

REQL includes first-class commands for project and graph analysis records:

```text
PROJECTS
ARTIFACTS WHERE artifact_type = "code" LIMIT 20
FRAGMENTS WHERE fragment_type = "heading" LIMIT 20
SYMBOLS TYPE Function WHERE name CONTAINS "compile" LIMIT 20
COMMUNITIES LIMIT 20
HUBS TYPE Topic,Concept,Function LIMIT 10
DELTAS LIMIT 10
CACHE STATUS
```

## Mental Model

Memory is treated as:

```text
Memory = Graph + Activation + Reinforcement + Salience + Provenance
```

The graph is not only a persistence format. It is the logical runtime:

- nodes represent code entities, artifacts, findings, and graph-analysis records;
- edges represent code structure, references, relationships, and provenance;
- weights guide ranking and propagation;
- positive polarity reinforces and negative polarity inhibits;
- direct matches and bounded graph paths determine which code records emerge;
- incremental compile, salience, and graph analysis keep the graph useful over time.

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
  -> archival of stale graph records
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
  -> cross-community bridge detection
  -> Community / Bridge nodes and hub properties
```

## Main Node Types

- `Community`: topological cluster.
- `Bridge`: specific evidence-backed connection between communities.
- `GraphDelta`: change trace.
- `Project`: scanned project root.
- `Directory`: deterministic project directory.
- `File`: deterministic project file.
- `Package`: package directory or module package marker.
- `SourceArtifact`: registered source, document, data, config, or binary file.
- `SourceFragment`: deterministic fragment derived from a source artifact.
- `ArtifactCacheEntry`: successful artifact compilation fingerprint.
- `CompilationRun`: one incremental compilation invocation.
- `Module`, `Function`, `Class`, `Interface`, `Method`, `Variable`, `Import`,
  `Dependency`, `Endpoint`, `Schema`, `Config`, `Test`, `Comment`,
  `Docstring`, `StaticAnalysisFinding`: compile-mode technical graph records.
  Unresolved calls are summarized on the owning symbol.

## Main Edge Types

- `ABOUT`
- `MENTIONS`
- `CO_OCCURS_WITH`
- `SUPPORTS`
- `EXPRESSES`
- `EXPLAINS`
- `EVIDENCED_BY`
- `SUPERSEDES`
- `COMPILED_IN`
- `PART_OF`
- `DERIVED_FROM`
- `SYNTHESIZES`
- `UPDATED_BY`
- `TRACKS`
- `PARTICIPATES_IN`
- `BELONGS_TO_COMMUNITY`
- `BRIDGES_COMMUNITY`
- `SUPPORTED_BY`
- `AFFECTED_BY_DELTA`
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
- `DECORATED_BY`
- `HANDLES_ROUTE`
- `HAS_FINDING`
- `TESTS`
- `CONFIGURES`

Every edge includes weight, confidence, polarity, origin, and additional
properties.

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

## Tests

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

The suite covers:

- CLI lifecycle;
- compile-time project scanning and artifact registration;
- incremental compilation, cache behavior, deletion archival, and graph deltas;
- code and document-fragment compilation;
- retrieval;
- salience and graph analysis;
- reports and export;
- block-file persistence;
- low-level store operations.

## Project Limitations

The compile-time scan layer registers artifact metadata before parsing content
during `project compile`. Project-specific exclusions live in `scan.exclude`
inside the loaded config YAML and support anchored, glob, and directory
patterns.

The incremental compiler creates deterministic `SourceFragment`, document
term, `RawEvent`, code-symbol, cache, run, and delta records by default. It does
not create embeddings or require model-backed extraction.

## License

MIT. See `LICENSE`.


