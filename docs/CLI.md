# CLI

```bash
python launcher.py
reql query_context --query "..."
reql query_explore --query "..." --view owners --view callers --json
reql query_explore --query "..." --serialization-paths-only
reql query_graph --query "..." --max-depth 2
reql query_graph --query "..." --json
reql query_memories --query "..." --limit 8 --json
reql inspect --node-id NODE_ID --json
reql stats
reql storage inspect
reql storage inspect --json
reql storage compact
reql project compile PATH
reql project update PATH
reql project compile . --watch
reql project exclude ".tmp/" "generated/*.json"
reql project status PATH
reql project report PATH --output reports/
reql cache status [PATH]
reql cache clear [PATH]
reql query "DELTAS LIMIT 10"
reql query "COMMUNITIES LIMIT 20"
reql query "HUBS LIMIT 20"
reql query "EXPLAIN HUB 'NODE_ID'" --json
reql inspect --node-id NODE_ID
reql export --out graph.json
reql export --json --out reql-json
reql export --html --out graph.html
reql export --html --json --out reql-graph-out
reql config show
reql config init
reql --set project.id=team-a config show
reql install codex --project
reql install claude --project
reql install --project
reql install codex,claude --dry-run
reql install claude --project --no-hooks
reql install codex --project --command-dir ~/.local/bin
reql uninstall codex,claude --project
reql-mcp
reql-mcp --read-only
reql-mcp --config conf.yaml --set project.id=team-a --read-only
reql-mcp --transport http --host 127.0.0.1 --port 8765 --api-key "change-this-key"
reql-mcp --transport http --host 0.0.0.0 --port 8765 --api-key "change-this-key" --read-only
```

From a source checkout, use `python cli.py ...` as the repository-local CLI
entry point. It adds `src` to Python's import path automatically, so it does not
require an editable install or a manual `PYTHONPATH` change.

Use `python launcher.py` when you want the guided terminal menu for common
operations, project workflows, graph analysis, exports, and MCP setup. Use
`python cli.py ...` or `reql ...` for command-line workflows and automation.

The menu can create or open graph storage, retrieve graph context, compose a
context block, execute REQL, show stats, generate reports, compile projects,
list/open/delete managed graph files under `.reql/`, switch the active storage
file, and print Codex/Claude MCP connection snippets.

`launcher.py` intentionally does not mirror the CLI command surface. It is an
interactive guided menu. Use `cli.py`, `reql`, and `reql-mcp` for scripted or
agent-facing command execution.

Use `--json` on supported commands to get machine-readable output.
By default, project and cache commands store data in
`<build path>/.reql/memory.reql`; other commands use `./.reql/memory.reql`
from the current working directory. Pass `--storage` to override this.
Top-level help lists canonical commands alphabetically. Nested command groups
summarize their subcommands in the main command list, for example `project`
shows `compile`, `update`, `status`, and `report`. Use `query_context`,
`query_explore`, `query_graph`, `query_memories`, and `query` as the supported
command names for those workflows.

`query_context` composes a deterministic agent-ready context block. It is
informative by default and renders one compact block with matching nodes, file
and line references, source links, and raw-query references for extended
research. Pass `--code`, `--docs`, or `--test` to limit the same query to code,
documentation/imported documents, or tests. Pass `--cleanup` for dead-code and
unused-symbol cleanup context; cleanup output shows only matching
`StaticAnalysisFinding` candidates. Add `--json` to get compact structured data
without duplicated rendered Markdown, including `query_mode`, `scopes`,
`owner_candidates`, `cleanup_candidates`, `working_set`, and `targeted_reads`.

`query_explore` is a codebase exploration mode for coding agents that need the
dependency chain before editing. It returns focused views for `owners`,
`callers`, `public_surface`, `serialization_paths`, `docs_mentions`, and `code`;
pass `--view owners --view code` for a tight function-level edit slice, pass
`--view` more than once to include a subset, or use shortcuts such as
`--owners-only`, `--code-only`, `--callers-only`, `--public-surface-only`,
`--serialization-paths-only`, `--docs-mentions-only`, and `--code-only`. Use
`--json` when another tool needs structured records with node ids, locations,
edge provenance, and the rendered `context`.

`query_graph` is the agent-oriented structured retrieval command. It finds
seed nodes from the query, expands the graph up to `--max-depth`, gathers
neighbor nodes, edges, and textual source fragments, filters isolated generic
nodes by default, and renders a compact
context block. Use `--json` to receive the full payload with `seed_nodes`,
`ranked_nodes`, `nodes`, `edges`, `edge_directions`, `sources`,
`filtered_node_ids`, and `counts`. Edge records preserve `from_id -> to_id`
and also include `source_id`, `target_id`, `directed`, and `direction`; the
`edge_directions` index groups each node's incoming and outgoing links for
context construction.
Use `--no-filter-generic` when debugging why a generic node was filtered.

`query_memories` is the unified compact retrieval command for agents. It uses
the same seed search and bounded graph expansion as the internal retrieval
pipeline, optionally includes connected source texts, deduplicates repeated text, and returns compact
memory rows with `id`, `type`, `label`, `text`, `score`, `rank`, `location`,
`source_for`, `source_for_label`, `relation`, `direction`, `edge_id`, and
`reasons`. With `--json`, the payload also includes retrieval metadata that
previously required separate lookup calls: `trace_id`, `seed_node_ids`,
`ranked_nodes`, `nodes`, `edges`, `edge_directions`, `sources`, `parameters`,
and `counts`. Use `--limit`, `--max-depth`, and `--max-text-chars` to keep
prompt input small; pass `--no-sources` when only ranked graph node text is
needed.

Use `query "RETRIEVE ... RETURN ..."` when you need explicit custom columns,
and prefer `query_context` for synthesized context with owner targets, snippets,
source evidence, or cleanup/edit sections.

`inspect --node-id NODE_ID --json` resolves an id printed by `query_memories`,
`query_graph`, or a REQL statement. It returns the node payload, a
normalized `location` object when file/source metadata is available,
source/location hints gathered from adjacent nodes and edges, and bounded
incoming/outgoing neighbor records. This is the preferred next step when an
agent needs to verify where a compact memory result came from.

Query and retrieval commands record usage in an append-only usage journal, not
as canonical graph rewrites. They do not persist `RetrievalTrace` nodes or
`USED_IN_CONTEXT` edges, but ranking can use the usage overlay immediately.
Read/query commands open the graph read-only directly, so multiple queries can
run concurrently. A compile/update writer waits for existing readers and blocks
new readers while it owns or is waiting on the writer lock.
Hub explanations are available through REQL statements such as
`reql query "EXPLAIN HUB 'node_id'" --json`.

`storage inspect` prints block-file diagnostics: block counts, record counts by
kind, compression ratio, dense-node count, manifest fields, WAL status, and
logical index sizes after replay. Use `--json` for exact values in scripts.
`storage compact` explicitly rewrites the current logical graph into a new
compact storage generation and reports blocks, records, bytes, and generation
id before and after the rewrite.

Set `diagnostics.enabled = true` and
`diagnostics.path: ".reql/profile.jsonl"` in `conf.yaml` to append structured
performance events for commands that run compile or retrieval work. The log is
JSONL and includes phase names such as `compile.scan`, `compile.plan`,
`compile.artifact`, `compile.transaction`, `retrieval.lexical_search`,
`retrieval.expand`, and `query.parse`, `query.evaluate`, and `graph.close`,
with durations and relevant counters.

`reql install` auto-detects supported coding-agent profiles by default and
writes assistant-facing REQL instructions for deterministic project memory.
Supported platforms are `codex`, `claude`, `opencode`, `kilo`, `cursor`, `gemini`,
`copilot`, `openclaw`, `hermes`, `kimi`, `antigravity`, and `agents`; use
explicit canonical platform names, `all`, or `--all` to override auto-detection.
`--project` writes project-local files such as `.codex/skills/reql-project/SKILL.md`,
`.claude/CLAUDE.md`, `AGENTS.md`, `GEMINI.md`, `.cursor/rules/reql.mdc`,
`.kilocode/rules/reql.md`, `.github/copilot-instructions.md`,
`.github/instructions/reql.instructions.md`, and agent-specific skill/rule
directories for OpenCode, OpenClaw, Hermes, Kimi Code, and Google Antigravity.
Without `--project`, the installer writes to the matching assistant profile under
the home directory. User-scope installs also add always-on profile instructions
where the agent supports them, such as `~/AGENTS.md` for Codex,
`~/.claude/CLAUDE.md` for Claude Code, `~/GEMINI.md` for Gemini CLI, and
agent-profile `AGENTS.md` or rule files for OpenCode, Kilo Code, OpenClaw,
Hermes, Kimi Code, Google Antigravity, Cursor, GitHub Copilot, and generic
AGENTS-compatible clients.
The command is idempotent and supports `--dry-run` and `--json`.
It also writes a REQL-owned `reql` command shim that points back to the current
checkout or installed package; pass `--command-dir` to select the shim
directory. Generated skills tell agents to prefer `reql`, then fall back to the
absolute shim path, then to the Python launcher command, so agents are not
dependent on a single discovery mechanism.
Skill-capable clients receive the generated project context skill
(`reql-project`). The generated skill includes `SKILL.md`, optional UI metadata
under `agents/openai.yaml`, and focused `references/` for bootstrap/query,
updates/watch mode, reports/exports/MCP, and optional document semantics.
Agents run `reql project status .`; if that reports `Project not found`, they
must immediately run `reql project compile .` before broad raw file exploration.
Generated project guidance treats REQL as the repository context index: agents
should not duplicate `query_context`, `query_memories`, or `query_graph` with
broad `rg`, recursive directory listings, custom scanners, or other
repository-wide discovery commands. Targeted file reads remain for exact edits,
debugging, and tests.
For unused-code cleanup, generated guidance tells agents to query deterministic
`FINDINGS` and `StaticAnalysisFinding` rows first, then validate candidates with
targeted source inspection or symbol searches before recommending removals.
This keeps direct cleanup such as unused imports or variables separate from
public/API-risk candidates such as functions, methods, classes, entry points,
callbacks, and dynamically referenced hooks. `FINDINGS` defaults to
`cleanup_rank` ordering, so high-priority product-code candidates are listed
before test-local or low-confidence findings.
Generated profile guidance also tells agents that `/reql` should load the
REQL workflow first, dirty `.reql/` files are expected after updates, and
`reports/GRAPH_REPORT.md` is for broad review when bounded queries are not
enough.
`reql project compile . --watch` is monitor mode for continuous updates during
active editing, not a reason to skip the one-shot bootstrap when no graph
exists. A running watcher performs an initial cache check, then monitors
filesystem changes and compiles only dirty or deleted artifacts, so agents
should query the maintained graph instead of starting repeated compile loops.
If no watcher is running, generated guidance tells agents to run
`reql project compile .` once after modifying project files before finishing, so
the graph reflects the completed edits.
Each platform install writes a `.reql_agent_version` stamp next to the installed
skill or integration files so future installs can update the generated content
cleanly. `reql uninstall` removes REQL-owned skill files, version stamps,
managed instruction sections, owned command shims, and automatic hooks while
preserving unrelated content in shared files. It does not overwrite or delete a
pre-existing `reql` command that was not generated by this installer. Claude
and Gemini installs register JSON settings hooks by default; pass `--no-hooks`
to install only the skill/instruction files.

`reql-mcp` starts the optional dependency-free MCP server for agent clients.
The default transport is stdio. Use `--transport http` with `--host`, `--port`,
and an API key from `--api-key` or `REQL_MCP_API_KEY` to share the server over
HTTP. See `docs/MCP.md` for the tool list, HTTP endpoint details, and Codex or
Claude Desktop configuration examples.

`export --html` writes a standalone browser view of the graph already stored in
memory. Use it to explore nodes, relations, neighbors, clusters, and source
paths without running a server. Large stores render a bounded connected visual
core for a dense, responsive browser view with no automatic movement. If `--out` points to a directory or
to a path without an `.html` suffix, the command writes `graph.html` inside that path.
Add `--json` to also write `graph.json` next to the HTML file.
Use `export --json` without `--html` to write `graph.json` to disk instead of
printing the JSON payload to stdout.

Use `--config path/to/conf.yaml` to load project settings, or set
`REQL_CONFIG` for agent wrappers. Use repeated global `--set
section.option=value` flags for generic overrides after the config file is
loaded. CLI flags such as `--max-file-size-mb` and `--output`
override matching config values for the command being run.
For project and cache commands that receive a `PATH`, REQL searches for
`conf.yaml` from that path upward, so each project can define its own config.
If none is found, it falls back to the canonical `conf.yaml` at the REQL code
root. Explicit `--config`, `REQL_CONFIG`, and `--set` still take precedence.

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
symbols, communities, hubs, bridge signals, and memory health.

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


