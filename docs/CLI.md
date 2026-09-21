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
reql project history . --limit 5
reql project diff .
reql project explain . --focus "payment workflow"
reql project pipeline .
reql project pipeline . --code --out docs/architecture

# Retrieve context
reql query_context --query "payment service"
reql query_context --query "payment service serializer" --code
reql query_explore --query "payment service serialization" --view owners --view code
reql query_graph --query "payment service" --max-depth 2 --json
reql query_memories --query "payment service" --limit 8 --json
reql inspect --node-id NODE_ID --json

# Coding-agent operational memory, with no copied project graph data
reql agent init
reql agent dashboard
reql agent session start "Serializer cleanup"
# Optional explicit activity scope (Codex uses CODEX_THREAD_ID automatically)
reql agent --agent AGENT_ID --activity TASK_ID session start "Serializer cleanup"
reql agent note add "Read the payment service serializer"
reql agent task add "Patch serializer error handling"
reql agent decision add "Reuse the existing graph store"
reql agent link TASK_ID DECISION_ID --relation implements
reql agent batch --link-many TASK_ID depends_on DECISION_ID,RISK_ID
reql agent batch --json agent-ops.json
reql agent handoff "Serializer cleanup ready for review"
reql agent export --json
reql agent export --json --metadata

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

## Repository Explanation

```bash
reql project explain .
reql project explain . --focus "payment retries"
reql project explain . --focus "payment retries" --max-capabilities 8 --max-workflows 4 --json
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
missing, run `reql project compile .` first.

## Project Pipeline Export

```bash
reql project pipeline .
reql project pipeline . --html
reql project pipeline . --code
reql project pipeline . --html --out reports/pipeline.html
reql project pipeline . --code --out reports/
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
directory. Pass `--storage` to override the graph store path.

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
`src/memory/cli.py`. The spec supplies the command path, access mode, snapshot
support, parser configurator, help text, and handler. `build_parser()` installs
registered specs into their parent parser group, while execution uses the same
selected spec to choose read-only versus mutating storage access, validate
`--snapshot`, and invoke the handler.

All commands that open `MemoryGraph` are registered this way. Commands with a
different lifecycle, such as installation, configuration bootstrap, agent
workspaces, watcher status, and direct block-storage inspection, keep their
dedicated execution paths without duplicating graph access classification.

## Agent Workspace

`reql agent` is the operational-memory layer used by REQL-aware coding-agent
integrations. Each CLI agent gets its own private memory store under
`.reql/agents/` and all agents share a small internal bus in
`.reql/agent-bus.reql`. Explicit agent selection has highest precedence;
otherwise a stable activity/thread id deterministically selects the private
workspace. An implicit default workspace is used only when selection is
unambiguous.
The canonical graph in `.reql/memory.reql` is the sole source of repository,
file, symbol, and relationship facts. Agent memory never copies, derives, links,
or synchronizes canonical graph records.

When assistant instructions or a REQL skill are installed, these commands are
normally invoked by the coding agent as part of its repository workflow. They
let the agent keep plans, findings, decisions, open tasks, risks, sessions, and
handoff summaries outside the model context window:

```bash
reql project compile .
reql agent init
reql agent dashboard
reql agent dashboard --post "scope: serializer owner identified" --kind stage
reql agent status
reql agent session start "Focused implementation pass"
```

`reql agent status` reports the current session as active only when it still has
open tasks. A completed or otherwise idle session is shown as the last idle
session, which keeps old session titles visible for recovery without making
them look like the current working focus.

`reql agent init` is idempotent: it never recreates an existing workspace.
`REQL_AGENT_ID`/`--agent` wins over activity-derived selection. Parallel
integrations normally need no manual flag because `REQL_AGENT_ACTIVITY_ID`,
`CODEX_THREAD_ID`, or `--activity` produces a stable project-scoped agent id.
Ambiguous activity-less selection fails clearly instead of following a global
bus pointer. `reql agent dashboard` returns `reql-agent-dashboard-v1` as the
routine attention index for intra-session, inter-session, and parallel work.
It includes the current and latest previous session, active tasks, recent
durable memory, currently working and recently finished agents, relevant bus
signals, and exact commands for deeper inspection. `--post TEXT` publishes one
checkpoint before the read and is limited to 240 characters. `dashboard
--agents` adds detailed cross-store state for each registered agent, including
its current session, open tasks, and recent decisions. Because it opens private
stores, this explicit option may wait on busy agents.

Current sessions remain scoped by activity.
REQL uses `REQL_AGENT_ACTIVITY_ID` first and `CODEX_THREAD_ID` second when
available; other clients can pass `--activity ACTIVITY_ID` explicitly. Without
an activity id, the agent has one current session.

The agent saves observations while working:

```bash
reql agent note add "Reviewed command routing and recorded the outcome"
reql agent finding add "The current plan needs a compatibility test"
reql agent decision add "Keep operational memory isolated per activity"
```

The agent may relate its own tasks, decisions, findings, notes, plans, and risks.
Only IDs printed by `agent list`, `agent search`, or an earlier agent command
are accepted:

```bash
reql agent task add "Implement agent reset"
reql agent link TASK_ID DECISION_ID --relation implements
reql agent batch --link-many TASK_ID depends_on DECISION_ID,RISK_ID
```

`agent session start "TITLE"` starts a new current working session and closes
the previous current session. New notes, tasks, decisions, findings, and agent
links are tagged with that session. Do not print `agent map` during normal
planning, editing, or verification; the active model already holds that state.
Use it only after context loss, compaction, a handoff, or a long pause:

```bash
reql agent map
reql agent map --session current
reql agent map --task TASK_ID
reql agent map --since 2026-06-29T12:00:00+00:00
```

`agent map --session current` limits recovery to the current session. You can
also pass a session id to recover an earlier session.

Use `agent batch` when several notes, decisions, tasks, findings, or links
should be written together under one Agent Workspace lock:

```bash
reql agent batch --json agent-ops.json
reql agent batch --task task="Patch CLI" --decision decision="Batch agent writes" --link '$task' implements '$decision' --json
```

`agent-ops.json` may be a JSON array or an object with an `operations` array:

```json
{
  "operations": [
    {"op": "task.add", "description": "Patch CLI", "as": "task"},
    {"op": "decision.add", "text": "Batch agent writes", "as": "decision"},
    {"op": "link", "from": "$task", "to": "$decision", "relation": "implements"}
  ]
}
```

Aliases declared with `as` can be referenced later in the same batch as
`$alias`. Supported operations are `note.add`, `task.add`, `task.done`,
`decision.add`, `finding.add`, `link`, and `link-many`.

For small planning batches, inline options avoid creating a temporary JSON
file. `--note`, `--task`, `--decision`, and `--finding` accept either `TEXT` or
`ALIAS=TEXT`; `--link FROM RELATION TO` creates one relation; `--link-many FROM
RELATION TARGETS` accepts comma-separated agent-owned targets.
Aliases from inline additions are referenced as `$alias` by later links.

List, search, inspect, and export operational memory:

```bash
reql agent list --type task --status open
reql agent search "reset behavior" --json
reql agent search "reset behavior" --json --metadata
reql agent show TASK_ID --json
reql agent export --json
reql agent export --json --metadata
```

Agents read or write the shared bus to coordinate without merging their private
memories:

```bash
reql agent bus
reql agent dashboard --post "Parser worker found the CLI owner" --target master
reql agent handoff "Parser worker done; review payload in bus"
```

Prefer `agent dashboard` for routine pipeline checkpoints and attention
routing. Publish terse `scope:`, `implement:`, `verify:`, or `blocked:` stage
transitions, then follow a printed `drill` command only when that item affects
the current task. Use `agent bus` for full bus listings and `dashboard
--agents` for a wider worker inventory.

Every coding agent must run this when its work pass ends:

```bash
reql agent finish "Focused tests passed; serializer fix ready"
```

`agent finish` snapshots a final handoff to the shared bus, closes the current
session, marks the agent completed so it disappears from the dashboard's
working roster, and removes its private store and sidecars. The compact final
handoff remains on the bus for `retention.days`. Reuse the identity with
`agent init` before starting another session.

`agent bus` lists registered agents, bus messages, and handoffs. Its JSON
output omits handoff payload snapshots by default so old handoffs stay compact;
pass `agent bus --include-payloads --json` only when you need the full saved
working-map payloads. `dashboard --post` stores a short shared message. `agent
handoff` snapshots this agent's current compact working map and publishes it to
the master bus, so the master can make choices from saved open tasks,
decisions, plans, risks, and essential relationships without opening the
worker's private store directly.

Use `dashboard --agents` for detailed agent state, `dashboard --post` for bus
updates, and `batch --link-many` for multi-target links. Operational notes use
typed `agent note add`; batch JSON uses `note.add`.

`agent list` keeps relation output focused on agent-created relations and,
when node filters are present, relations connected to the listed nodes.
For recovery only, `agent map` separates two kinds of context:

- `Agent memory` contains bounded decisions, findings, notes, risks, and plans,
  with their originating session ids;
- `Current session` and `Previous sessions` contain compact activity summaries,
  counts, and up to six highlights instead of replaying raw session history.

The JSON form exposes these domains under `context.learned` and
`context.sessions`, identified by `context_format: reql-agent-context-v3`.
Compact top-level `open_tasks`, `decisions`, and `relations` remain available.
Timestamps and raw operational metadata remain
omitted unless a command explicitly requests metadata.
Use `agent map --task TASK_ID` to recover one task and agent items connected
to it by agent-created relations. Use `agent map --session current` to recover
the current working session without remembering a task id. Use `agent map
--session current --completed` only when recovering a completed session and its
operational relations.
Use `agent map --since TIMESTAMP` to show only agent items or relations updated
inside a time window. If another process holds the agent store lock, commands
retry briefly and then report that the Agent Workspace is busy, including the
lock wait budget that was exhausted.

Agent commands emit lifecycle progress on stderr: an immediate `started` line,
periodic `still running` heartbeats, and a terminal `completed` or `failed`
line with elapsed time. Completions after eight seconds are explicitly marked
as late but final, so a slow storage open is not mistaken for an unfinished
operation. JSON remains isolated on stdout. Use `reql agent --no-progress
COMMAND ...` when a caller requires silent stderr.

During recovery, use `agent map --metadata` only when timestamps or stored
operational metadata are necessary. `agent search --metadata` and
`agent export --metadata` expose the equivalent detailed fields for their own
workflows.

Reset discards agent-created notes, tasks, decisions, findings, plans, risks,
sessions, and relationships without reading or changing the canonical graph:

```bash
reql agent reset
```

Supported agent item types are `note`, `task`, `decision`, `finding`, `risk`,
`plan`, and `session`. Supported agent relationship types are `depends_on`,
`blocks`, `implements`, `touches`, `explains`, `derived_from`, `related_to`,
`replaces`, and `conflicts_with`. Commands that return structured output support
`--json`; list/search support filters such as `--type`, `--status`,
`--relation`, `--since`, and `--limit` where relevant.

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
reql query_context --query "readme FAQ"
reql query_context --query "unused imports" --cleanup
reql query "FINDINGS WHERE finding_type = 'possibly_orphan_directory' RETURN relative_path,file_count,files,cleanup_priority"
```

It is informative by default and returns matching nodes, file/line references,
source links, owner candidates, cleanup candidates, working-set records, and
targeted reads. Use `--code`, `--docs`, or `--test` to limit context to a
scope. For small code working sets, exact phrase matches in `SourceFragment`
nodes can be rendered as snippets so agents can use precise source spans before
opening full files. In rendered code context, read-plan spans and inspection
commands are folded into `Code results`; the structured JSON retains the full
`read_plan`. Use `--cleanup` for dead-code and unused-symbol cleanup. Cleanup output
is always conservative and shows safe-remove findings plus medium-priority
directory aggregate findings. `possibly_orphan_directory` groups multiple
isolated code files under one containing directory with `file_count` and
`files`, so cleanup review does not start from one suggestion per file.
Validation-required findings remain available through explicit `FINDINGS`
REQL statements rather than the removal-oriented context builder.

CLI, Python, and MCP use the same `QueryContextRequest` contract and canonical
budget defaults (`top_k=20`, `max_depth=3`, `max_items=20`). JSON output is the
canonical `ContextResult` envelope: `schema_version`, `graph_revision`, and
`confidence` are top-level fields, while trace metadata and projected context
are nested under `payload`.

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
source hints.

For coding-agent edits, `query_context --code` renders owner symbols,
bounded source spans, snippets when they are small enough, and research
commands. JSON output also includes `read_plan` and `change_chain` so another
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
non-mutating `query` statements open a consistent read-only index snapshot, so
parallel readers can run together. Compile/update writers wait for existing
readers and block new readers while opening the write session.
Use `reql storage locks` to see the owning command, lock duration, process
liveness, watcher state, stale status, and snapshot availability. Add
`--recover-stale` for conservative cleanup of dead same-host owners. If a live
writer must remain active, place the global `--snapshot` option before a
read-only command, for example `reql --snapshot query_context --query "FAQ"`.
Expected storage failures return exit code `1` without a Python traceback. A
writer lock prints only one recovery command in the form `reql is locked for
write: to fix any possible stale: reql --storage "PATH" storage locks
--recover-stale`.

## Inspection and Export

```bash
reql stats
reql storage inspect
reql storage inspect --json
reql storage locks
reql storage locks --recover-stale
reql storage compact
reql storage clear [PATH]
reql storage clear [PATH] --json
reql export --out graph.json
reql export --json --out reql-json
reql export --html --out graph.html
reql export --html --json --out reql-graph-out
```

`storage inspect` prints block-file diagnostics such as block counts, record
counts, compression ratio, dense-node count, manifest fields, WAL status, and
logical index sizes. `storage compact` rewrites the current logical graph into a
new compact storage generation.

Successful `project compile` and `project update` commands automatically prune
project-owned history, archived records, and usage events older than
`retention.days`. The current graph and newest successful run, delta, and
revision are always retained. Cleanup counts appear in JSON output and in human
output when data was removed.

`storage clear [PATH]` performs a clean build of the current project in a
temporary store, then atomically replaces the selected `memory.reql` only after
compilation succeeds. It regenerates `artifact-cache.json` and discards graph
history, archived/deleted records, the old WAL, and the query-usage journal. A
failed clean build preserves both the existing store and cache. `PATH` defaults
to the current directory and controls the default
`<PATH>/.reql/memory.reql`; an explicit `--storage` path is replaced in full, so
do not target a store shared by unrelated projects.

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
directories. `reql-agent` covers compile/query/report/update workflows for the
standard project graph and Agent Workspace commands such as `reql agent init`,
routine coordination through `agent dashboard`, `agent task add`, `agent link`,
recovery via `agent map`, cleanup via `agent finish`, `agent export --json`, and
`agent reset`. Pass `--project-dir` to target another project root. Pass
`--user` to write to matching assistant profiles under the home directory.

Generated `SKILL.md` files keep a 20–30 line fast path for status, bounded
retrieval, targeted reads, edits, tests, and routing. Bootstrap, query variants,
graph refresh, reports, documents, and Agent Workspace guidance live only in
routed `references/` files and are loaded when that situation occurs. Platform rules
for Cursor, Copilot, Kilo, and shared instruction files are rendered from the
same canonical rule set so their behavior does not drift or repeat.

The installer also writes a REQL-owned `reql` command shim; use `--command-dir`
to select the shim directory. Claude and Gemini hooks are installed by default
and can be skipped with `--no-hooks`. `reql uninstall` removes REQL-owned skill
files, version stamps, managed instruction sections, owned command shims, and
automatic hooks while preserving unrelated content in shared files.

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
reql --set scan.max_file_size_mb=2 --set cache.enabled=false project compile .
reql project compile . --watch
```

`reql.conf` can configure scan limits, include globs, strict scoped exclusions, cache behavior,
compile document ingestion, graph analysis toggles, and the default report
output directory. Set `scan.use_gitignore: true` to apply the project root
`.gitignore` together with the joined `scan.exclude` rules.
Set `scan.ignore_defaults: true` when the project's `scan.include` and
`scan.exclude` lists must replace, rather than extend, the internal defaults.

Document formats are disabled by default. Enable only what the project needs:

```yaml
compile:
  documents:
    markdown: true
    json: true
```

The `compile.document_formats` registry associates those names with their
extensions and optional filenames and can also be overridden per project.

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
reql project exclude .tmp/ generated/*.json
reql project exclude vendor/ --path PATH
reql project status . --json
reql project history . --limit 10
reql project diff .
reql project report . --output reports/
reql project pipeline .
```

`project compile` scans read-only first, registers files as graph artifacts,
compares fingerprints against the compilation cache, and then compiles changed
artifacts. It creates or updates project/artifact metadata and parses supported
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

When `reql.conf` is present, project compile/update, watch mode, and cache
status apply configured `scan.include` and `scan.exclude` patterns. Compile
exclusions should be listed in `scan.exclude`. With
`compile.ingest_documents=true`, text documents become
structural `SourceFragment` records for provenance and query context.
`compile.documents` toggles formats by name; the internal
`compile.document_formats` registry supplies their extensions and filenames.
Ingested documents also pass through the local deterministic document
processor. It creates ranked document `Concept` nodes, underlying `RawEvent`
observation nodes, `MENTIONS`, `EVIDENCED_BY`, `DERIVED_FROM`, and
`CO_OCCURS_WITH` edges, and `REFERENCES` edges from document terms to compiled
code symbols when a fragment explicitly names a symbol. This path runs inside
`project compile` and does not require model, agent, or coding-agent calls.

Nested subdirectories may define their own `reql.conf`; during a parent scan,
their `scan.exclude` rules apply only to that subdirectory tree.

`project exclude PATTERN [PATTERN ...]` creates or updates the selected
project's `reql.conf` and appends patterns to `scan.exclude`. Use it only for
explicit exclusions or obvious dependency/cache/build-output directories. It
defaults to the current working directory, accepts `--path PATH` when the
runtime project path is elsewhere, preserves existing config values, and skips
rules that are already present. Generated YAML uses unquoted plain values when
they are unambiguous. `./` is significant: it anchors a rule to that config's
directory; without it, the rule matches at every depth. A trailing `/` is only
presentation and does not affect duplicate detection. The only wildcard form
is `*suffix` in the final segment (`*.json`, `generated/*.json`, or their
`./`-anchored equivalents). Unsupported glob forms, absolute paths, `..`, empty
segments, and backslashes are rejected.

`project report` writes `GRAPH_REPORT.md`, `GRAPH_DELTAS.md`, and
`CACHE_REPORT.md` to the selected output directory. The reports summarize
project structure, compilation cache, graph deltas, artifact ingestion, code
symbols, communities, hubs, and memory health.

## Incremental compilation

```bash
reql project compile .
reql project update .
reql project watch-status .
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
Cache metadata for a compilation run is flushed once as an atomic batch, so
cold compilation remains linear in the number of changed artifacts instead of
rewriting the growing JSON cache once per file. Bounded `FIND` queries use the
storage type/status/property indexes for candidate counts and top-K selection;
equality filters such as `WHERE relative_path = 'src/app.py'` avoid a full graph
scan.
Compile mode applies built-in default ignore rules for dependency, VCS, cache,
build-output, and local database paths, then applies configured
include/exclude patterns and file-size limits.

`project watch-status [PATH]` checks a dedicated watcher lease without opening
the graph. Monitor mode opens the canonical graph only for a compile batch and
releases it while idle, so readers and agents remain responsive. It
reports `running`, `stopped`, `stale`, or `unknown`, plus PID, process liveness,
start time, duration, and command when available. Add `--json` for automation;
when monitor mode uses an explicit global `--storage`, pass the same option to
`watch-status`.

Use `project compile . --watch` from the working directory while Codex, Claude, or
another coding agent is actively changing files. The watcher uses Python
`watchdog` filesystem events, checks the same incremental cache, and runs
compilation only when dirty or deleted artifacts are detected. This is monitor
mode: keep it running during active work instead of launching repeated manual
compile/rebuild loops. It keeps running until interrupted. Use
`--watch-interval` as the bounded wait timeout for scripted runs,
`--watch-debounce` to coalesce event bursts, and `--watch-iterations` for
bounded automation, or pass an explicit path when the workspace is elsewhere.
Only one watcher lease may be active for a project. Files are hash-verified
against the scan used for compilation; a changing tree is rescanned up to three
times without publishing a mismatched revision or cache entry.

Markdown, plain text, and PDF artifacts are parsed when their
`compile.documents.<format>` toggle is `true`.
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

Use `storage clear [PATH]` when the desired result is instead equivalent to
deleting the project-local REQL store and compiling the current checkout from
scratch.

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


