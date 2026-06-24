# CLI

This page is the command reference for the installed `reql` command, the
repository-local `python cli.py ...` entry point, the guided `launcher.py`, and
the optional `reql-mcp` server.

## Quick Reference

```bash
# Build or refresh the graph
reql project compile .
reql project update .
reql project compile . --watch
reql cache status .

# Retrieve context
reql query_context --query "payment service"
reql query_explore --query "payment service serialization" --view owners --view code
reql query_graph --query "payment service" --max-depth 2 --json
reql query_memories --query "payment service" --limit 8 --json
reql inspect --node-id NODE_ID --json

# Query, inspect, report, and export
reql query "DELTAS LIMIT 10"
reql query "HUBS LIMIT 20"
reql query "EXPLAIN HUB 'NODE_ID'" --json
reql stats
reql storage inspect --json
reql project report . --output reports/
reql export --html --json --out reql-graph-out

# Configuration and integrations
reql config show
reql --set project.id=team-a config show
reql install codex
reql install codex --user
reql uninstall codex,claude
reql-mcp --read-only
```

## Entry Points

Use `reql ...` for normal command-line automation. From a source checkout,
`python cli.py ...` exposes the same command surface and adds `src` to Python's
import path automatically, so it does not require an editable install or manual
`PYTHONPATH` changes.

Use `python launcher.py` for an interactive menu that can open graph storage,
compile projects, retrieve context, run REQL queries, generate reports, export
graphs, and print MCP configuration snippets. `launcher.py` intentionally does
not mirror every CLI flag; use `reql`, `python cli.py`, or `reql-mcp` for
scripts and agent-facing command execution.

## Storage, Output, and Help

Project and cache commands default to `<build path>/.reql/memory.reql`.
Other commands default to `./.reql/memory.reql` from the current working
directory. Pass `--storage` to override the graph store path.

Use `--json` on supported commands for machine-readable output. Top-level help
lists canonical commands alphabetically, and nested command groups summarize
their subcommands:

```bash
reql --help
reql project --help
reql query_context --help
```

## Retrieval Commands

`query_context` composes a deterministic agent-ready context block:

```bash
reql query_context --query "payment service"
reql query_context --query "payment service" --code --json
reql query_context --query "unused imports" --cleanup
```

It is informative by default and returns matching nodes, file/line references,
source links, owner candidates, cleanup candidates, working-set records, and
targeted reads. Use `--code`, `--docs`, or `--test` to limit context to a
scope. Use `--cleanup` for dead-code and unused-symbol cleanup; cleanup output
shows matching `StaticAnalysisFinding` candidates before removals.

`query_explore` returns dependency slices for concrete code targets:

```bash
reql query_explore --query "payment service serialization" --view owners --view callers --json
reql query_explore --query "payment service serialization" --serialization-paths-only
```

Views include `owners`, `callers`, `public_surface`, `serialization_paths`,
`docs_mentions`, and `code`. Use repeated `--view` flags or shortcut flags to
keep output small.

`query_graph` returns a structured query-centered subgraph:

```bash
reql query_graph --query "payment service" --max-depth 2
reql query_graph --query "payment service" --json
```

The JSON payload includes seed nodes, ranked nodes, edges, edge directions,
linked sources, filtered-node diagnostics, and counts. Use
`--no-filter-generic` when debugging why a generic node was filtered.

`query_memories` returns compact ranked text rows:

```bash
reql query_memories --query "payment service" --limit 8 --json
reql query_memories --query "payment service" --no-sources
```

It uses the same seed search and bounded graph expansion as the retrieval
pipeline. JSON output includes trace id, seed ids, ranked nodes, nodes, edges,
sources, parameters, and counts.

Use `inspect --node-id NODE_ID --json` to resolve a node id printed by
retrieval or REQL statements and inspect its location, adjacent records, and
source hints.

Query/retrieval commands write usage events to an append-only journal rather
than rewriting canonical graph records. Read/query commands open the graph
read-only, so parallel readers can run together. Compile/update writers wait
for existing readers and block new readers while opening the write session.

## Inspection and Export

```bash
reql stats
reql storage inspect
reql storage inspect --json
reql storage compact
reql export --out graph.json
reql export --json --out reql-json
reql export --html --out graph.html
reql export --html --json --out reql-graph-out
```

`storage inspect` prints block-file diagnostics such as block counts, record
counts, compression ratio, dense-node count, manifest fields, WAL status, and
logical index sizes. `storage compact` rewrites the current logical graph into a
new compact storage generation.

`export --html` writes a standalone browser view of the graph. If `--out`
points to a directory or to a path without an `.html` suffix, the command writes
`graph.html` inside that path. Add `--json` to also write `graph.json` next to
the HTML file. Use `export --json` without `--html` to write JSON graph data to
disk instead of stdout.

## Assistant Installs

```bash
reql install codex
reql install claude
reql install
reql install codex,claude --dry-run
reql install claude --no-hooks
reql install codex --command-dir ~/.local/bin
reql install codex --user
reql uninstall codex,claude
```

`reql install` auto-detects supported coding-agent profiles by default and
writes assistant-facing REQL instructions for deterministic project memory.
Supported platforms are `codex`, `claude`, `opencode`, `kilo`, `cursor`,
`gemini`, `copilot`, `openclaw`, `hermes`, `kimi`, `antigravity`, and
`agents`; use explicit canonical platform names, `all`, or `--all` to override
auto-detection.

By default, installs write project-local files such as
`.codex/skills/reql-project/SKILL.md`, `.claude/CLAUDE.md`, `AGENTS.md`,
`GEMINI.md`, `.cursor/rules/reql.mdc`, `.kilocode/rules/reql.md`, and
agent-specific skill/rule directories. Pass `--project-dir` to target another
project root. Pass `--user` to write to matching assistant profiles under the
home directory.

The installer also writes a REQL-owned `reql` command shim; use `--command-dir`
to select the shim directory. Claude and Gemini hooks are installed by default
and can be skipped with `--no-hooks`. `reql uninstall` removes REQL-owned skill
files, version stamps, managed instruction sections, owned command shims, and
automatic hooks while preserving unrelated content in shared files.

## MCP Server

```bash
reql-mcp
reql-mcp --read-only
reql-mcp --config conf.yaml --set project.id=team-a --read-only
reql-mcp --transport http --host 127.0.0.1 --port 8765 --api-key "change-this-key"
reql-mcp --transport http --host 0.0.0.0 --port 8765 --api-key "change-this-key" --read-only
```

`reql-mcp` starts the optional dependency-free MCP server for agent clients. The
default transport is stdio. Use `--transport http` with `--host`, `--port`, and
an API key from `--api-key` or `REQL_MCP_API_KEY` to share the server over HTTP.
See [MCP.md](MCP.md) for tools, endpoint details, and client configuration.

## Diagnostics

Set `diagnostics.enabled = true` and
`diagnostics.path: ".reql/profile.jsonl"` in `conf.yaml` to append structured
performance events for commands that run compile or retrieval work. The JSONL
log includes phases such as `compile.scan`, `compile.plan`,
`compile.artifact`, `compile.transaction`, `retrieval.lexical_search`,
`retrieval.expand`, `query.parse`, `query.evaluate`, and `graph.close`, with
durations and relevant counters.

## Configuration

```bash
reql config init
reql config show
reql --set scan.max_file_size_mb=2 --set cache.enabled=false project compile .
reql project compile . --watch
```

`conf.yaml` can configure scan limits, include/exclude globs, cache behavior,
compile document ingestion, graph analysis toggles, and the default report
output directory.

## REQL

```bash
reql query "PROJECTS"
reql query "ARTIFACTS WHERE artifact_type = 'code' LIMIT 20"
reql query "SYMBOLS TYPE Function WHERE name CONTAINS 'compile' LIMIT 20"
reql query "RETRIEVE 'office plant' LIMIT 8 RETURN id,type,text,score,relative_path,line_start"
reql query "FIND nodes WHERE text ILIKE '%office plant%' LIMIT 10"
reql query "CACHE STATUS"
reql query "HUBS LIMIT 20"
```

`query` executes the REQL engine. REQL has first-class commands for projects,
artifacts, fragments, code symbols, communities, hubs, deltas, and cache status,
in addition to generic `FIND`, `MATCH`, `PATH`, `SEARCH`,
`RETRIEVE`, and `EXPLAIN`. `WHERE` supports SQL-like text, range, list, and
null operators such as `LIKE`, `ILIKE`, `REGEX`, `BETWEEN`, `IN`, and `IS NULL`;
see [REQL.md](REQL.md) for examples.
Use `reql query "RETRIEVE ... RETURN ..."` when `query_memories --json` is too
coarse and callers need custom deterministic columns such as source paths, line
ranges, or scores.

## Project Compilation

```bash
reql project compile .
reql project compile . --max-file-size-mb 5
reql project exclude ".tmp/" "generated/*.json"
reql project exclude "vendor/" --path PATH
reql project status . --json
reql project report . --output reports/
```

`project compile` scans read-only first, registers files as graph artifacts,
compares fingerprints against the compilation cache, and then compiles changed
artifacts. It creates or updates project/artifact metadata and parses supported
content into queryable graph nodes. Image and video files are skipped and are
not registered as artifacts.

When `conf.yaml` is present, project compile/update, watch mode, and cache
status apply configured `scan.include` and `scan.exclude` patterns. Compile
exclusions should be listed in `scan.exclude`. With
`compile.ingest_documents=true`, text documents become
structural `SourceFragment` records for provenance and query context.
`compile.documents` controls which concrete document formats and extensions are
ingested. Ingested documents also pass through the local deterministic document
processor. It creates ranked document `Concept` nodes, underlying `RawEvent`
observation nodes, `MENTIONS`, `EVIDENCED_BY`, `DERIVED_FROM`, and
`CO_OCCURS_WITH` edges, and `REFERENCES` edges from document terms to compiled
code symbols when a fragment explicitly names a symbol. This path runs inside
`project compile` and does not require model, agent, or coding-agent calls.

`project exclude PATTERN [PATTERN ...]` creates or updates the selected
project's `conf.yaml` and appends patterns to `scan.exclude`. Use it only for
explicit exclusions or obvious dependency/cache/build-output directories. It
defaults to the current working directory, accepts `--path PATH` when the
runtime project path is elsewhere, preserves existing config values, and skips
rules that are already present. Pass all patterns in one command, do not use
workspace-wide patterns such as `*`, `**`, or `**/*`, and do not exclude
source/framework roots needed for the task.

`project report` writes `GRAPH_REPORT.md`, `GRAPH_DELTAS.md`, and
`CACHE_REPORT.md` to the selected output directory. The reports summarize
project structure, compilation cache, graph deltas, artifact ingestion, code
symbols, communities, hubs, and memory health.

## Incremental compilation

```bash
reql project compile .
reql project update .
reql cache status .
reql cache clear .
reql query "DELTAS LIMIT 10"
reql query "DELTAS WHERE id = 'delta:...' LIMIT 1" --json
```

`project compile` scans read-only first, compares fingerprints against the
project-local `.reql/artifact-cache.json` cache, then writes only changed and
deleted artifact deltas. `ArtifactCacheEntry` graph nodes are written for
query/report inspection and recovery. Unchanged `Project`, `Directory`,
`File`, and `SourceArtifact` records are not rewritten. Changed artifacts
compile into a complete indexed graph ready for query as soon as the command
returns. Deleted files archive their `SourceArtifact`, `File`, and related
fragment/code nodes. Use
`project update` for a manual incremental refresh of the same path.
Compile mode applies built-in default ignore rules for dependency, VCS, cache,
build-output, and local database paths, then applies configured
include/exclude patterns and file-size limits.

Use `project compile . --watch` from the working directory while Codex, Claude, or
another coding agent is actively changing files. The watcher uses Python
`watchdog` filesystem events, checks the same incremental cache, and runs
compilation only when dirty or deleted artifacts are detected. This is monitor
mode: keep it running during active work instead of launching repeated manual
compile/rebuild loops. It keeps running until interrupted. Use
`--watch-interval` as the bounded wait timeout for scripted runs,
`--watch-debounce` to coalesce event bursts, and `--watch-iterations` for
bounded automation, or pass an explicit path when the workspace is elsewhere.

Markdown, plain text, and PDF artifacts are parsed by document parsers when
their `compile.documents` policy has `ingest: true`.
Markdown creates heading, paragraph, list, code block, table, and link
structure. Plain text is chunked by paragraph. PDF parsing uses
optional dependencies when installed and otherwise stores parser errors plus
metadata-only fragments without failing the full compile. Image and video files
are ignored by compile.

Every compile invocation persists a `CompilationRun` node and a compilation
`GraphDelta` node. Use `reql query "DELTAS ..."` to inspect persisted deltas.

`cache clear` archives cache metadata in the project `.reql` directory and the
graph cache entries for the project path. It does not delete or archive graph
data, artifacts, or fragments.

## Graph analysis

```bash
reql query "COMMUNITIES LIMIT 20"
reql query "COMMUNITIES LIMIT 20" --json
reql query "HUBS LIMIT 20"
reql query "HUBS TYPE Function,Class LIMIT 10"
reql query "HUBS LIMIT 20" --json
reql query "EXPLAIN HUB 'NODE_ID'" --json
```

`COMMUNITIES` runs deterministic lightweight community detection and writes
`Community` nodes plus `BELONGS_TO_COMMUNITY` and `BRIDGES_COMMUNITY` edges.

`HUBS` scores central, specific, useful nodes. Generic high-degree nodes are
penalized, and each ranked node receives `hub_score`, `centrality_score`,
`specificity_score`, `community_bridge_score`, `is_hub`, `hub_rank`, and
`hub_reason` properties.


