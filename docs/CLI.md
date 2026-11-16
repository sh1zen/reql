# CLI

This page is the command reference for the installed `reql` command, the
repository-local `python cli.py ...` entry point, the guided `launcher.py`, and
the optional `reql-mcp` server.

## Quick Reference

```bash
# Build or refresh the graph
reql project compile
reql project compile --watch
reql cache status
reql project history --limit 5
reql project diff
reql project explain --focus "payment workflow"
reql project pipeline
reql project pipeline --code --out docs/architecture

# Retrieve context
reql query_context --query "payment service"
reql query_context --query "payment service serializer" --code
reql query_explore --query "payment service serialization" --view owners --view code
reql query_graph --query "payment service" --max-depth 2 --json
reql query_memories --query "payment service" --limit 8 --json
reql inspect --node-id NODE_ID --json

# Coding-agent operational memory, with no copied project graph data
reql agent init --name "Serializer cleanup"
reql agent dashboard
reql project overview
# Optional explicit activity scope (Codex uses CODEX_THREAD_ID automatically)
reql agent --agent AGENT_ID --activity ACTIVITY_ID init --name "Serializer cleanup"
reql agent note "Read the payment service serializer"
reql agent record task "Patch serializer error handling" --file src/serializer.py --status in_progress
reql agent task list
reql agent task list --all
reql agent record decision "Preserve empty fields" --parent TASK_ID --why "Consumers distinguish absent and empty"
reql agent finish "Serializer cleanup ready for review" --task TASK_ID --revision N
reql agent list
reql agent search "serializer error handling"

# Query, inspect, report, and export
reql query "DELTAS LIMIT 10"
reql query "HUBS LIMIT 20"
reql query "EXPLAIN HUB 'NODE_ID'" --json
reql stats
reql storage inspect --json
reql project report --output reports/
reql export --html --json --out reql-graph-out

# Configuration and integrations
reql config show
reql config set retention.agent_sessions 30
reql install codex
reql install codex --user
reql uninstall codex,claude
reql-mcp --read-only
```

Use the host's activity id for `ACTIVITY_ID`. Replace record-id placeholders and
`N` with the ids and observed revisions returned by REQL.

## Repository Explanation

```bash
reql project explain
reql project explain --focus "payment retries"
reql project explain --focus "payment retries" --max-capabilities 8 --max-workflows 4 --json
```

`project explain` reads an already compiled project and returns a
business-oriented view of its code:

- capabilities inferred from cohesive module and symbol ownership;
- interface, application, domain, core, and infrastructure layers;
- semantic workflows with intent, trigger, inputs, outputs, invariants, explicit
  `implemented_by` participants, and corroborating graph/document evidence;
- code owners, primary paths, dependencies, and associated tests;
- focus-specific starting points for planning a change.

The command is read-only. It computes the view on demand from deterministic
graph facts and does not create capability nodes or require model calls. Use
`--json` for the versioned structured payload or omit it for Markdown. Focus
ranks workflow candidates but does not create triggers. If the project is
missing, run `reql project compile` first.

## Project Pipeline Export

```bash
reql project pipeline
reql project pipeline --html
reql project pipeline --code
reql project pipeline --html --out reports/pipeline.html
reql project pipeline --code --out reports/
```

`project pipeline` reads an already compiled project and writes a deterministic
high-level flow view. It admits explicit routes, handlers, commands, public API
boundaries, and conventional process entrypoints; if none exist, public roots
of the call graph are marked as inferred triggers. Calls, route handling,
instantiation, wrappers, resolved imports, writes, returns, emissions, and
raises provide the evidence. Test-local symbols are excluded, while private
symbols needed to connect an observed flow remain available in component
details.

The default format is interactive HTML and the default destination is
`pipeline.html` in the registered project root. `--code` selects Mermaid and
writes `pipeline.mmd`; `--html` is the explicit HTML selector and cannot be
combined with `--code`. An `--out` directory receives the default filename, or
you may pass a matching `.html`/`.htm` or `.mmd`/`.mermaid` file. Relative
explicit output paths are resolved from the current directory. Existing files
are replaced atomically and the command prints only the absolute output path.

The HTML view embeds the projection data and uses the same pinned
`vis-network` CDN runtime as the generic graph export. It supports pan/zoom,
fit/reset, search, workflow and architectural-layer filters, and source-symbol
inspection. Mermaid output is a `flowchart LR` with shared components, observed
outcomes, source-location comments, and dashed feedback edges. Runtime-only or
dynamic paths are never invented; an observed terminal means only that static
evidence ends there. The command does not compile missing projects or open a
browser automatically.

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
directory. The CLI always uses these project-local locations.

Use `--json` on supported commands for machine-readable output. Top-level help
lists canonical commands alphabetically, and nested command groups summarize
their subcommands:

```bash
reql --help
reql project --help
reql query_context --help
```

### Declarative command registration

New leaf commands should be described by a `CommandSpec` in
`src/memory/cli.py`. The spec supplies the command path, access mode, parser
configurator, help text, and handler. `build_parser()` installs
registered specs into their parent parser group, while execution uses the same
selected spec to choose read-only versus mutating storage access and invoke the
handler.

All commands that open `MemoryGraph` are registered this way. Commands with a
different lifecycle, such as installation, configuration bootstrap, agent
workspaces, watcher status, and direct block-storage inspection, keep their
dedicated execution paths without duplicating graph access classification.

## Agent Workspace

Shared work persists goals, tasks, decisions and outcomes across agents and
sessions. Private stores hold scratch notes and active session state. See
[Engineering coordination](COORDINATION.md) for record fields, readiness,
revision checks, batch recovery and retention.

```bash
reql project overview
reql project overview --details --limit 10
reql project context --task WORK_ID --json
reql project context --file src/module.py --query "contract"
reql agent init --name "Contract update"
reql agent record task "Validate consumers" --file src/module.py
reql agent record --id TASK_ID --revision N --status in_progress
reql agent show TASK_ID --json
reql agent finish "Consumer checks passed" --task TASK_ID --revision N
```

Replace ids and revisions with returned values. Creation uses `agent record KIND
CONTENT`; updates can omit unchanged kind/content. Fields include `--key`,
`--status`, `--why`, repeated `--file`, `--workstream`, `--parent`, repeated
`--depends-on`, `--contradicts`, `--supersedes`, `--summarizes`, `--next` and
`--importance 1..3`.

`project context` accepts `--query`, repeated `--file`, `--workstream`, `--task`,
`--history`, `--limit 1..40` and `--json`. It reads shared work without selecting
a private agent or opening the project graph. `project overview --limit 1..20`
bounds each section; `--details` expands the architecture and reads available
private sessions for per-agent details. JSON also includes their retained notes.
Text marks incomplete records and gives `agent show ID --json` for full evidence.

For assigned work, `agent init --name NAME --task ID --revision N` claims the
observed revision and returns scoped context. Assigned `agent finish` completes
that task and returns a checkpoint; plain finish closes the session without
completing tasks. Rejected completion leaves the session open.

`agent batch --file - --name NAME --finish SUMMARY` reads a UTF-8 JSON array of
1–40 record objects from stdin. A project-local file can replace `-`; input is
limited to 512,000 characters. Objects use Python field names such as `record_id`,
`expected_revision`, `rationale` and `next_action`. Request-local `ref` values
allow links to earlier results. Each record is atomic, but the whole batch is not:
failures return committed receipts and stop subsequent operations. See
[batch semantics](COORDINATION.md#planning-and-continuation) before retrying.

Finish receipts contain a checkpoint id/revision; recover with `project context
--task CHECKPOINT_ID`. Plain writes return compact receipts, while `--json`
preserves the documented structured result. `agent show ID --json` includes
revision history.

Identity selection prefers `--agent`/`REQL_AGENT_ID` over activity-derived
identity (`--activity`, `REQL_AGENT_ACTIVITY_ID`, `CODEX_THREAD_ID`). Ambiguous
private selection fails with guidance. `agent dashboard` shows owned work and
private notes; `agent task list --all` and `agent list --all` include retained
completed entries.

`agent note` is private scratch; `note --agent ID` is directed scratch and
`note --public` publishes a dashboard message. `agent search` searches work and
messages with attribution. `agent export --metadata` includes owned shared work
and private session records. `agent terminate ID` closes an agent's activities;
`agent reset` discards private scratch while preserving shared work.

Progress goes to stderr and JSON to stdout. Use `agent --no-progress COMMAND`
to suppress progress. Managed parallel-work ownership and independent claim/finish
workflows are described in the [coordination guide](COORDINATION.md#planning-and-continuation).

## Retrieval Commands

When the project-relative path is already known, bypass semantic retrieval and
graph expansion with `locate`:

```bash
reql locate "extensions/wp-optimizer/readme"
reql locate "extensions/wp-optimizer/readme.txt" --json
```

The lookup is case-insensitive and index-backed. If the input has no extension,
REQL checks the exact path and the known documentation suffixes `.md`,
`.markdown`, `.txt`, and `.rst`.

`query_context` composes a deterministic agent-ready context block:

```bash
reql query_context --query "payment service"
reql query_context --query "payment service" --code --json
reql query_context --query "payment service" --code --source
reql query_context --query "readme FAQ"
reql query_context --query "unused imports" --cleanup
reql query "FINDINGS WHERE finding_type = 'possibly_orphan_directory' RETURN relative_path,file_count,files,cleanup_priority"
```

It is informative by default and returns matching nodes, file/line references,
source links, owner candidates, cleanup candidates, working-set records, and
targeted reads. Use `--code`, `--docs`, or `--test` to limit context to a
scope. Default code text lists files, owners, associated tests, and the small
source previews already retrieved. Its bounded **Change boundaries** section shows
indexed `WRAPS`, `OVERRIDES`, `INHERITS`, caller and public-surface relations near
the selected owners. `payload.impact.implementation_links` retains source/target
references and edge ids/types in JSON. These are static relationships to verify
in current source, not instructions to create another wrapper or override. Missing
static callers do not prove that a symbol has no users.
When a source path is displayed, symbol labels omit its exact dotted module
prefix. Class/method names and file locations remain visible; JSON keeps full
qualified names. Labels without that path context remain fully qualified.

Previews preserve indentation and complete
lines within 16 lines/900 characters. Their ranges describe actual disk text;
`requested_line_end`, `truncated` and `next_line` identify any missing part.
Graph fallback text is labeled as indexed graph evidence and never counts as
complete current source. Preview reads share the 4 MiB file ceiling used by
verified inspection and reuse one file snapshot per request. JSON retains the full `read_plan`.
Structured read-plan commands retain `--json` for existing machine consumers.
The text lists the primary inspection command for each file when current source
is still needed, so obtaining a node id does not require repeating the query as JSON.

Follow its existing `inspect --node-id ID --limit 1` command when source
is missing. Plain `inspect` shows location, verified source, source links and
adjacent relations without repeating their serialized properties. It labels
unverified indexed text and marks truncation. Use `--json` for the complete node,
location, `source_excerpt`, source and relation fields. The excerpt
uses the same hash checks, project confinement and source budgets described
below. Stale or unavailable files carry a reason without an old source body;
nodes without a code/test span have `source_excerpt: null`. CLI, Python and MCP
share the structured result without any new option. Explicit `--json` preserves
the full inspection contract. `--limit` still controls relations,
not source length. Read-plan inspection commands request one relation per edge
direction rather than thirty for a source read; general inspection keeps its defaults.

Add `--source` with `--code` or `--test` to embed current source bodies directly.
Python and MCP expose the same opt-in `include_source=True` field. This requires
explicit code/test scopes without docs. Overlapping spans are merged, each file
is read once, and the response shares limits of three files, six regions,
120 source lines, and 6,000 source characters. Files larger than 4 MiB are not
read in full. Source paths must resolve inside their registered project root,
even when the graph store or current directory is elsewhere.

JSON `snippets` report `source_status` (`verified`, `stale`, or `unavailable`).
Verified bodies match both artifact metadata and a committed revision manifest.
`line_end` describes the actual excerpt; `truncated`, `requested_line_end`, and
`next_line` identify incomplete spans. `read_plan.snippet_embedded` is true only
when verified text covers the requested span. Reuse complete excerpts; read
uncovered ranges directly. Changed or missing files, uncommitted metadata, and
read failures return a reason without falling back to old graph text. Detected
source changes mark graph freshness stale unless a refresh is already running.
Queries do not compile or mutate the graph. Files can change after capture.

Use `--cleanup` for dead-code and unused-symbol cleanup. Cleanup output
is always conservative and shows safe-remove findings plus medium-priority
directory aggregate findings. `possibly_orphan_directory` groups multiple
isolated code files under one containing directory with `file_count` and
`files`, so cleanup review does not start from one suggestion per file.
Validation-required findings remain available through explicit `FINDINGS`
REQL statements rather than the removal-oriented context builder.

CLI, Python, and MCP use the same `QueryContextRequest` contract and canonical
budget defaults (`top_k=20`, `max_depth=3`, `max_items=20`). JSON output is the
canonical `ContextResult` envelope: `schema_version`, `graph_revision`,
`source_revision`, `freshness` and `confidence` are top-level fields. Trace
metadata and projected context are nested under `payload`.
In text, the displayed file path supplies its module identity; `(module)` avoids
repeating the qualified module name. Tests already shown in `Files` are identified
as such in `Associated tests`, without repeating their file rows.

For an unscoped informative query, REQL also detects cross-layer file
relationships when documentation and implementation files in the same project
share significant query terms. Structured JSON exposes those records as
`related_files`; they are omitted from the compact rendered context. Translation
catalogs (`.pot` and `.po`) from that project are
included when the correlated change can affect translatable code strings.

`query_explore` returns dependency slices for concrete code targets:

```bash
reql query_explore --query "payment service serialization" --view owners --view callers --json
reql query_explore --query "payment service serialization" --serialization-paths-only
reql query_explore --query "profile template" --structural-duplicates-only
```

Views include `owners`, `callers`, `public_surface`, `serialization_paths`,
`docs_mentions`, `structural_duplicates`, and `code`. The structural duplicate
view compares template markup hierarchy, parent-child relations, attributes,
and tag sequences rather than lexical file relevance. It is opt-in by default;
use repeated `--view` flags or shortcut flags to keep output small.

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
current source excerpt when its code/test span is verifiable. Generated read
plans use `--limit 1` to keep relation output bounded for this source lookup.

For coding-agent edits, `query_context --code --source` renders owner symbols,
bounded source spans, associated tests, and verified current source excerpts.
JSON output also includes `read_plan` and `change_chain` so another
agent can follow the intended intervention path without opening whole files or
patching around missing project context.

Dotted field queries such as `Field.type` also return an end-to-end
`data_trace`. REQL seeds these queries from both symbols and code-body source
fragments, resolves typed parameter field reads across imported models, and
orders matching locations into recognizable layers such as prompts, wire
models, validation, persisted models, graph projections, reconciliation,
OpenAPI compilation, and tests or fixtures. The rendered context shows the
same records under `End-to-end data trace`. The `serialization_paths` explore
view follows `READS`, `WRITES`, `RETURNS`, `RAISES`, `REFERENCES`, and source
evidence for the requested depth instead of stopping after the first adjacent
edge.

Query/retrieval commands write usage events to an append-only journal rather
than rewriting canonical graph records. `project status`, `query_context`, and
non-mutating `query` statements open the index read-only, so parallel readers
can run together. Compile writers wait for existing readers and block new
readers while opening the write session.
Use `reql storage locks` to see the owning command, lock duration, process
liveness, watcher state, and stale status. Add `--recover-stale` for
conservative cleanup of dead same-host owners. Read-only commands respect a
live writer's lock. Expected storage failures return exit code `1` without a
Python traceback. A writer lock prints the recovery command
`reql storage locks --recover-stale`.

## Inspection and Export

```bash
reql stats
reql storage inspect
reql storage inspect --json
reql storage locks
reql storage locks --recover-stale
reql storage compact
reql storage clear
reql storage clear --json
reql export --out graph.json
reql export --json --out reql-json
reql export --html --out graph.html
reql export --html --json --out reql-graph-out
```

`storage inspect` prints block-file diagnostics such as block counts, record
counts, compression ratio, dense-node count, manifest fields, WAL status, and
logical index sizes. `storage compact` rewrites the current logical graph into a
new compact storage generation.

When `project compile` creates a changed-manifest
`ProjectRevision`, it is a REQL commit. REQL retains the latest
`retention.commits` commits (default `20`) and automatically prunes older
project-owned history, archived records, and usage events at that commit
boundary. Clean compile invocations do not advance the retention window. The
current graph and retained commits' runs, deltas, and revisions remain
available. Cleanup counts appear in JSON output and in human output when data
was removed.

`storage clear` performs a clean build of the current project in a
temporary store, then atomically replaces the selected `memory.reql` only after
compilation succeeds. It regenerates `artifact-cache.json` and discards graph
history, archived/deleted records, the old WAL, and the query-usage journal. A
failed clean build preserves both the existing store and cache. `PATH` defaults
to the current directory and controls the default `<PATH>/.reql/memory.reql`.

`export --html` writes a standalone browser view of the graph. If `--out`
points to a directory or to a path without an `.html` suffix, the command writes
`graph.html` inside that path. Add `--json` to also write `graph.json` next to
the HTML file. The browser view starts from detected entry points and expands
or collapses one connected depth per node click; test-local nodes are omitted
from this visual projection. Use `export --json` without `--html` to write the
complete JSON graph data to disk instead of stdout.

## Assistant Installs

```bash
reql install codex
reql install claude
reql install
reql install codex,claude --dry-run
reql install claude --no-hooks
reql install codex --command-dir ~/.local/bin
reql install codex --user
reql uninstall
reql uninstall codex,claude
```

`reql install` and `reql uninstall` auto-detect supported coding-agent profiles
by default and write or remove assistant-facing REQL instructions for
deterministic project memory.
Supported platforms are `codex`, `claude`, `opencode`, `kilo`, `cursor`,
`gemini`, `copilot`, `openclaw`, `hermes`, `kimi`, `antigravity`, and
`agents`; use explicit canonical platform names, `all`, or `--all` to override
auto-detection. Auto-detection only uses real assistant profile signals or
assistant commands whose install targets are known. Generic root directories
such as `.codex`, `.github`, and `.agents` are ignored. Codex detection requires
paths such as `.codex/skills` or `.codex/hooks.json`. Copilot detection requires
Copilot-specific paths such as `~/.copilot/skills`,
`.github/copilot-instructions.md`, or `.github/instructions`. Generic
Agent-Skills detection uses `~/.agents/skills` or `./.agents/skills`. Project
installs only inspect project-local signals, and user installs only inspect
home-scope signals and user commands, so a profile under `C:\Users\...` cannot
cause a project install or uninstall under another drive. When no profile is
detected in an interactive terminal, REQL prints the available disks and asks
for the coding-agent profile disk or path. It retries auto-detection there
first; only if that path has no supported profile does it ask which platform to
install or uninstall. The project is resolved later from the directory where
the agent is launched. In non-interactive scripts or CI, it exits and asks for
an explicit platform such as `reql install codex --user` or
`reql uninstall codex --user`.

By default, installs write project-local files such as
`.codex/skills/reql-agent/SKILL.md`, `.claude/CLAUDE.md`, `AGENTS.md`,
`GEMINI.md`, `.cursor/rules/reql.mdc`, `.kilocode/rules/reql.md`,
`.agents/skills/reql-agent/SKILL.md`, and agent-specific skill/rule
directories. `reql-agent` uses the project graph to bound repository discovery,
then directs the coding agent to verify the current source, callers, contracts,
and tests. For multi-step or resumed work it also covers Agent Workspace
commands described in [Engineering coordination](COORDINATION.md). Small
self-contained tasks skip Agent Workspace.
Pass `--project-dir` to target another project root. Pass
`--user` to write to matching assistant profiles under the home directory.

Generated `SKILL.md` files keep a concise fast path for status, bounded
retrieval, source verification, edits, tests, refresh, and routing. Agents reuse
the graph-defined working set and avoid repeated full-project scans. Bootstrap,
query variants,
graph refresh, reports, documents, and Agent Workspace guidance live only in
routed `references/` files and are loaded when that situation occurs. Platform rules
for Cursor, Copilot, Kilo, and shared instruction files are rendered from the
same canonical rule set so their behavior does not drift or repeat.

The installer also adds the explicit-only `reql-context-compact` skill. It runs a
local JSON transcript filter only when the host can export and replace its
message list. It retains every message and replaces only large, exact duplicate
tool results with references. It cannot compact the live Codex window or another
host without transcript replacement support. An external harness can run:

```bash
python -m agents.context_compact < transcript.json > compacted.json
```

The input file remains unchanged. Generated skills use short instructions; source
identifiers and command spelling remain exact.

The installer also writes a REQL-owned `reql` command shim; use `--command-dir`
to select the shim directory. Claude and Gemini hooks are installed by default
and can be skipped with `--no-hooks`. `reql uninstall` removes REQL-owned skill
files, version stamps, managed instruction sections, and automatic hooks while
preserving unrelated content in shared files. The shared command shim is kept
by default because other project or user integrations may still use it. Pass
the same explicit `--command-dir` used during installation when the shim itself
should also be removed.

## MCP Server

```bash
reql-mcp
reql-mcp --read-only
reql-mcp --config reql.conf --set project.id=team-a --read-only
reql-mcp --transport http --host 127.0.0.1 --port 8765 --api-key "change-this-key"
reql-mcp --transport http --host 0.0.0.0 --port 8765 --api-key "change-this-key" --read-only
```

`reql-mcp` starts the optional dependency-free MCP server for agent clients. The
default transport is stdio. Use `--transport http` with `--host`, `--port`, and
an API key from `--api-key` or `REQL_MCP_API_KEY` to share the server over HTTP.
See [MCP.md](MCP.md) for tools, endpoint details, and client configuration.

## Diagnostics

Set `diagnostics.enabled = true` and
`diagnostics.path: ".reql/profile.jsonl"` in `reql.conf` to append structured
performance events for commands that run compile or retrieval work. The JSONL
log includes phases such as `compile.scan`, `compile.plan`,
`compile.artifact`, `compile.transaction`, `retrieval.lexical_search`,
`retrieval.expand`, `query.parse`, `query.evaluate`, and `graph.close`, with
durations and relevant counters.

## Configuration

```bash
reql config init
reql config show
reql project compile --watch
```

`config init` creates a project `reql.conf`; `config show` prints effective
settings. `reql config set compile.documents.markdown true` updates a validated
local option. Document formats are disabled by default.

See [Configuration](CONFIGURATION.md) for the settings schema, scan patterns,
document policies, nested configuration, environment overrides and precedence.

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
reql project compile
reql project compile --if-stopped
reql project compile --max-file-size-mb 5
reql project status --json
reql project history --limit 10
reql project diff
reql project report --output reports/
reql project pipeline
```

`project status` reads registered metadata without scanning the filesystem.
It reports `Last compilation: completed`, `failed`, `running`, or `none`.
JSON adds `last_compilation` (null before compilation), containing the run id,
status, start/completion timestamps, total error count, and up to five errors.
A registered project still returns success after a failed compile; inspect this
field before treating its graph as usable. Status does not verify unobserved
filesystem edits.

`project compile` opens validated storage, scans the project read-only,
compares fingerprints against the compilation cache, then registers and compiles
changed artifacts. It creates or updates project/artifact metadata and parses supported
content into queryable graph nodes. Image and video files are skipped and are
not registered as artifacts.

Every successful tree change also creates an immutable `ProjectRevision`. A
revision stores a content-addressed tree hash, its parent revision, and file
transitions (`added`, `modified`, or `deleted`) with old and new SHA-256 values.
No-op compiles do not create revisions. Use `project history` for the
newest-first chain and `project diff` (optionally `--revision ID`) for one
revision's file changes. This is revision metadata for context and auditing; it
does not store file contents or modify the repository's own Git history.

After each compile or watch update, the CLI also prints a bounded verification
summary with changed files, added/updated/archived code symbols, and associated
test files. Test associations come from compiled graph relationships and the
conventional `tests/test_<module>.py` path when present. The complete structured
summary is available as `CompileProjectResult.to_dict()["summary"]`.

Compile, watch and cache commands use the configured scan policy. Put exclusions
in `scan.exclude`; see [Configuration](CONFIGURATION.md#behavior) for the supported
patterns and nested scopes. Enabled document formats produce the fragments,
terms and relations described in [Artifact ingestion](ARTIFACT_INGESTION.md).

`project report` writes `GRAPH_REPORT.md`, `GRAPH_DELTAS.md`, and
`CACHE_REPORT.md` to the selected output directory. The reports summarize
project structure, compilation cache, graph deltas, artifact ingestion, code
symbols, communities, hubs, and memory health.

## Incremental compilation

```bash
reql project compile
reql project compile --if-stopped
reql project compile --watch --watch-debounce 0.1
reql project watch-status --json
reql cache status
reql cache clear
reql query "DELTAS LIMIT 10"
```

Project commands operate on the current working directory. `--max-file-size-mb`
overrides the configured scan limit. Watch mode uses `watchdog`; install the
`watch` extra when needed. `--watch-interval` bounds the wait between checks,
`--watch-debounce` coalesces events, and `--watch-iterations` bounds a scripted run.
Without an iteration limit, monitoring continues until interrupted.

One watcher lease owns refresh for a project. `project watch-status` reads that
lease without opening the graph and reports `running`, `stopped`, `stale` or
`unknown`. `project compile --if-stopped` compiles only when stopped, leaves a
running watcher responsible and rejects uncertain ownership. It cannot be
combined with `--watch`.

`cache clear` archives compilation-cache metadata without deleting source graph
records. Matching successful compilation provenance can recover missing entries;
clearing the cache is not a clean graph rebuild. Use
[storage maintenance](STORAGE.md#inspection-compaction-and-clean-rebuilds) when
history and archived graph state must be discarded.

See [Incremental compilation](INCREMENTAL_COMPILATION.md) for stage order, cache
keys, partial failures, deletion handling and revision history. Parser and document
formats are owned by [Code analysis](CODE_ANALYSIS.md) and
[Artifact ingestion](ARTIFACT_INGESTION.md).

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
